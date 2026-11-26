from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from patients.models import (
    Medicine, MedicineBatch, StockMovement, PrescriptionItem, ConsultationSession,
    Patient, PatientBill, PharmacyAudit, PharmacyIssue, PharmacyPurchase, PharmacyReturn,
    PharmacySale, PharmacySettings, PharmacySupplier, StockTransfer,
)
from .operations import (
    batch_usable_quantity, cancel_sale, create_patient_return, create_sale, handover_issue,
    hold_quarantine, receive_purchase, receive_stock, record_damage, release_quarantine,
    reserve_issue, return_to_supplier, set_bill_status, settle_patient_return, transfer_stock,
    usable_batches, record_purchase_order, receive_ordered_purchase, set_purchase_line_details,
)

User = get_user_model()


def staff(name, role):
    user = User.objects.create_user(username=name)
    user.profile.designation = role
    user.profile.save()
    return user


class PharmacyWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.pharmacist = staff("ws_pharmacist", "pharmacy")
        cls.cashier = staff("ws_cashier", "registration_desk")
        cls.doctor = staff("ws_doctor", "doctor")
        cls.service_user = staff("ws_service", "service_staff")
        cls.admin = staff("ws_admin", "admin")
        cls.patient = Patient.objects.create(first_name="Sale", last_name="Customer", uhid="WS-1")
        cls.consultation = ConsultationSession.objects.create(
            patient=cls.patient, doctor=cls.doctor, visit_token="WS-VISIT", status="in_progress")
        cls.otc = Medicine.objects.create(name="Paracetamol OTC", prescription_required=False,
                                          gst_rate=Decimal("5.00"), pack_size=10)
        cls.rx_medicine = Medicine.objects.create(name="Amoxicillin Rx", pack_size=10)
        cls.rx = PrescriptionItem.objects.create(
            consultation=cls.consultation, medicine=cls.rx_medicine,
            medicine_name=cls.rx_medicine.name, prescribed_quantity=10, released_at=timezone.now())

    def batch(self, medicine=None, number="B1", days=120, units=20, cost="4.00", price="6.00", **extra):
        medicine = medicine or self.otc
        return receive_stock(
            user=self.pharmacist, medicine=medicine, batch_number=number,
            expiry_date=timezone.localdate() + timedelta(days=days), supplier="Acme Pharma",
            invoice_reference="INV-1", purchase_cost=Decimal(cost), selling_price=Decimal(price),
            quantity_units=units, request_key=uuid4(), **extra)

    # ── Direct sales / POS ────────────────────────────────────────────────

    def test_direct_sale_decrements_stock_and_writes_ledger(self):
        batch = self.batch(units=10)
        sale = create_sale(
            user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk,
                                               "quantity": 2}],
            discount="0", amount_received="12.60", payment_method="cash", request_key=uuid4())
        self.assertEqual(sale.status, PharmacySale.Status.COMPLETED)
        self.assertEqual(sale.subtotal, Decimal("12.00"))
        self.assertEqual(sale.tax, Decimal("0.60"))
        self.assertEqual(sale.total, Decimal("12.60"))
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 8)
        movement = StockMovement.objects.get(kind="sale")
        self.assertEqual(movement.quantity, -2)
        self.assertEqual(PharmacyAudit.objects.filter(action="sale_completed").count(), 1)

    def test_direct_sale_is_idempotent_and_bounded_by_stock(self):
        batch = self.batch(units=3)
        key = uuid4()
        first = create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                              "batch_id": batch.pk, "quantity": 2}],
                            amount_received="12.60", request_key=key)
        second = create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                               "batch_id": batch.pk, "quantity": 2}],
                             amount_received="12.60", request_key=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PharmacySale.objects.count(), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 1)
        with self.assertRaises(ValidationError):
            create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                          "batch_id": batch.pk, "quantity": 2}],
                        amount_received="12.60", request_key=uuid4())

    def test_prescription_only_medicine_and_short_payment_are_rejected(self):
        batch = self.batch(medicine=self.rx_medicine, number="RX-1")
        with self.assertRaises(ValidationError):
            create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.rx_medicine.pk,
                                                          "batch_id": batch.pk, "quantity": 1}],
                        amount_received="6.00", request_key=uuid4())
        cash_batch = self.batch()
        with self.assertRaises(ValidationError):
            create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                          "batch_id": cash_batch.pk, "quantity": 1}],
                        amount_received="1.00", request_key=uuid4())
        self.assertFalse(PharmacySale.objects.exists())

    def test_discount_limit_and_disabled_direct_sales(self):
        batch = self.batch()
        with self.assertRaises(ValidationError):
            create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                          "batch_id": batch.pk, "quantity": 1}],
                        discount="50.00", amount_received="0.00", request_key=uuid4())
        settings = PharmacySettings.current()
        settings.allow_direct_sales = False
        settings.save()
        with self.assertRaises(ValidationError):
            create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                          "batch_id": batch.pk, "quantity": 1}],
                        amount_received="6.30", request_key=uuid4())

    def test_cancelling_a_sale_restores_stock_once(self):
        batch = self.batch()
        sale = create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                             "batch_id": batch.pk, "quantity": 3}],
                           amount_received="18.90", request_key=uuid4())
        cancel_sale(user=self.pharmacist, sale=sale, reason="Customer returned")
        cancel_sale(user=self.pharmacist, sale=sale, reason="Customer returned")
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 20)
        self.assertEqual(StockMovement.objects.filter(kind="reversal").count(), 1)
        sale.refresh_from_db()
        self.assertEqual(sale.status, PharmacySale.Status.CANCELLED)

    def test_expired_quarantined_and_damaged_batches_are_never_sellable(self):
        fresh = self.batch(number="FRESH", days=200)
        expired = MedicineBatch.objects.create(
            medicine=self.otc, batch_number="OLD", expiry_date=timezone.localdate() - timedelta(days=1),
            supplier="Acme", invoice_reference="INV-OLD", purchase_cost=Decimal("1"),
            selling_price=Decimal("2"), on_hand=10)
        self.assertEqual(batch_usable_quantity(expired), 0)
        self.assertNotIn(expired, usable_batches(self.otc))
        self.assertIn(fresh, usable_batches(self.otc))
        MedicineBatch.objects.filter(pk=fresh.pk).update(quarantined=5)
        self.assertEqual(batch_usable_quantity(MedicineBatch.objects.get(pk=fresh.pk)), 15)
        with self.assertRaises(ValidationError):
            create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                          "batch_id": expired.pk, "quantity": 1}],
                        amount_received="2.00", request_key=uuid4())

    def test_a_sale_with_returns_cannot_be_cancelled(self):
        batch = self.batch()
        sale = create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                             "batch_id": batch.pk, "quantity": 2}],
                           amount_received="12.60", request_key=uuid4())
        create_patient_return(user=self.pharmacist, line=sale.lines.get(), quantity=1,
                              reason="Damaged pack", request_key=uuid4())
        with self.assertRaises(ValidationError):
            cancel_sale(user=self.pharmacist, sale=sale, reason="Voided")
        sale.refresh_from_db()
        self.assertEqual(sale.status, PharmacySale.Status.COMPLETED)

    def test_pos_patient_selection_survives_cart_updates_and_filters_medicines(self):
        self.batch(units=5)
        self.client.force_login(self.pharmacist)
        selected = self.client.get(reverse("pharmacy_pos"), {"patient": self.patient.pk})
        self.assertEqual(selected.context["patient"], self.patient)
        self.assertContains(selected, "Selling to")
        add = self.client.post(reverse("pharmacy_pos_add"),
                               {"batch": MedicineBatch.objects.first().pk, "quantity": 1})
        self.assertEqual(add.status_code, 302)
        page = self.client.get(reverse("pharmacy_pos"))
        self.assertContains(page, self.patient.uhid)
        self.assertEqual(page.context["patient"], self.patient)
        cleared = self.client.get(reverse("pharmacy_pos"), {"patient": ""})
        self.assertNotContains(cleared, "Selling to")
        self.assertIsNone(cleared.context["patient"])

    def test_pos_renders_prescription_queue_and_loads_inline_preparation_and_issue(self):
        self.batch(medicine=self.rx_medicine, number="RX-POS-DESK", units=20)
        self.client.force_login(self.pharmacist)
        response = self.client.get(reverse("pharmacy_pos"), {
            "tab": "prepare", "consultation": self.consultation.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="ph-desk-queue-strip ph-panel"')
        self.assertContains(response, 'class="ph-panel ph-inline-prepare"')
        self.assertContains(response, reverse("pharmacy_consultation", args=[self.consultation.pk]) + "?next=pos")
        self.assertContains(response, f"consultation={self.consultation.pk}")
        self.assertEqual(response.context["queue_tabs"], [
            ("ready", "Ready", 0), ("prepare", "To prepare", 1), ("payment", "Awaiting payment", 0),
        ])
        self.assertEqual(response.context["selected_consultation"], self.consultation)
        self.assertTrue(response.context["can_prepare"])
        row = response.context["prescription_rows"][0]
        self.assertEqual(list(row.matches), [self.rx_medicine])
        self.assertEqual((row.entered_quantity, row.available_stock, row.prepare_limit), ("0", 20, 10))

        prepared = self.client.post(reverse("pharmacy_consultation", args=[self.consultation.pk]) + "?next=pos", {
            "request_key": uuid4(), f"medicine_{self.rx.pk}": self.rx_medicine.pk,
            f"quantity_{self.rx.pk}": "4",
        })
        issue = PharmacyIssue.objects.get()
        self.assertRedirects(prepared, reverse("pharmacy_pos") + f"?issue={issue.pk}",
                             fetch_redirect_response=False)
        other_due = PatientBill.objects.create(
            patient=self.patient, source="registration", description="Other outstanding bill", amount=25,
        )
        issue_page = self.client.get(reverse("pharmacy_pos"), {"tab": "payment", "issue": issue.pk})
        self.assertEqual(issue_page.status_code, 200)
        self.assertContains(issue_page, 'class="ph-panel ph-inline-pay"')
        self.assertContains(issue_page, reverse("pharmacy_issue", args=[issue.pk]) + "?next=pos")
        self.assertContains(issue_page, 'name="action" value="pay"')
        self.assertEqual(issue_page.context["selected_issue"], issue)
        self.assertEqual(list(issue_page.context["lines"]), [issue.lines.get()])
        self.assertEqual(list(issue_page.context["other_dues"]), [other_due])

    def test_pharmacist_can_collect_issue_payment_and_hand_over_from_pos(self):
        batch = self.batch(medicine=self.rx_medicine, number="RX-POS-PAY", units=10)
        issue = reserve_issue(
            user=self.pharmacist, consultation=self.consultation,
            entries=[{"prescription_id": self.rx.pk, "medicine_id": self.rx_medicine.pk, "quantity": 3}],
            request_key=uuid4(),
        )
        self.client.force_login(self.pharmacist)
        self.assertContains(self.client.get(reverse("pharmacy_issue", args=[issue.pk])),
                            'name="action" value="pay"')
        paid = self.client.post(reverse("pharmacy_issue", args=[issue.pk]) + "?next=pos", {
            "action": "pay", "payment_method": "upi", "payment_reference": "UPI-123",
        })
        self.assertRedirects(paid, reverse("pharmacy_pos") + f"?issue={issue.pk}",
                             fetch_redirect_response=False)
        issue.bill.refresh_from_db()
        self.assertEqual((issue.bill.status, issue.bill.payment_method, issue.bill.payment_reference),
                         ("paid", "upi", "UPI-123"))
        self.assertEqual(issue.bill.paid_by, self.pharmacist)
        ready = self.client.get(reverse("pharmacy_pos"), {"issue": issue.pk})
        self.assertContains(ready, "Confirm medicine handover")
        self.assertNotContains(ready, 'name="action" value="pay"')

        handed_over = self.client.post(reverse("pharmacy_issue", args=[issue.pk]) + "?next=pos", {
            "action": "handover",
        })
        self.assertRedirects(handed_over, reverse("pharmacy_pos") + f"?issue={issue.pk}",
                             fetch_redirect_response=False)
        issue.refresh_from_db()
        batch.refresh_from_db()
        self.rx.refresh_from_db()
        self.assertEqual(issue.status, PharmacyIssue.Status.DISPENSED)
        self.assertEqual((batch.on_hand, batch.reserved), (7, 0))
        self.assertEqual((self.rx.dispensed_quantity, self.rx.reserved_quantity), (3, 0))

    def test_pharmacy_sidebar_hides_admin_links_from_pharmacists(self):
        admin_labels = (
            "Reports &amp; analytics", "Suppliers", "Purchase orders", "Purchase entry",
            "Stock adjustments", "Stock transfers", "Pharmacy settings", "Audit history",
        )
        request = RequestFactory().get(reverse("pharmacy_pos"))
        request.user = self.pharmacist
        pharmacist_sidebar = render_to_string("core/_pharmacy_sidebar.html", {"request": request})
        for label in admin_labels:
            self.assertNotIn(label, pharmacist_sidebar)
        for label in ("Dispense &amp; POS", "Sales history", "Dispensing history",
                      "Returns &amp; refunds", "Medicine master", "Stock &amp; batches",
                      "Receive stock", "Expiry management"):
            self.assertIn(label, pharmacist_sidebar)

        request.user = self.admin
        admin_sidebar = render_to_string("core/_pharmacy_sidebar.html", {"request": request})
        for label in admin_labels:
            self.assertIn(label, admin_sidebar)

    def test_pos_lists_only_otc_and_respects_disabled_direct_sales(self):
        otc_batch = self.batch(units=5)
        self.batch(medicine=self.rx_medicine, number="RX-1", units=5)
        self.client.force_login(self.pharmacist)
        page = self.client.get(reverse("pharmacy_pos"))
        self.assertContains(page, self.otc.name)
        self.assertNotContains(page, self.rx_medicine.name)
        settings = PharmacySettings.current()
        settings.allow_direct_sales = False
        settings.save()
        response = self.client.post(reverse("pharmacy_pos_add"),
                                    {"batch": otc_batch.pk, "quantity": 1}, follow=True)
        self.assertContains(response, "Direct sales are disabled")
        self.assertNotIn("pharmacy_sale_cart", self.client.session)

    def test_controlled_medicines_need_a_documented_authorisation(self):
        controlled = Medicine.objects.create(
            name="Morphine OTC", prescription_required=False, is_controlled=True, pack_size=5)
        batch = self.batch(medicine=controlled, number="CTRL-1", units=3)
        with self.assertRaises(ValidationError):
            create_sale(user=self.pharmacist,
                        line_specs=[{"medicine_id": controlled.pk, "batch_id": batch.pk, "quantity": 1}],
                        amount_received="6.30", request_key=uuid4())
        sale = create_sale(user=self.pharmacist,
                           line_specs=[{"medicine_id": controlled.pk, "batch_id": batch.pk, "quantity": 1}],
                           amount_received="6.30", controlled_authorisation="Dr Rao / PC-7781",
                           request_key=uuid4())
        self.assertEqual(sale.controlled_authorisation, "Dr Rao / PC-7781")
        self.assertEqual(PharmacyAudit.objects.get(action="sale_completed").after["controlled_authorisation"],
                         "Dr Rao / PC-7781")
        settings = PharmacySettings.current()
        settings.require_controlled_authorisation = False
        settings.save()
        create_sale(user=self.pharmacist,
                    line_specs=[{"medicine_id": controlled.pk, "batch_id": batch.pk, "quantity": 1}],
                    amount_received="6.30", request_key=uuid4())

    def test_return_and_sale_cancel_through_the_web_forms(self):
        batch = self.batch(units=8)
        sale = create_sale(user=self.pharmacist,
                           line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk, "quantity": 4}],
                           amount_received="25.20", request_key=uuid4())
        line = sale.lines.get()
        self.client.force_login(self.pharmacist)
        key = uuid4()
        page = self.client.post(reverse("pharmacy_return_new"), {
            "sale_line": line.pk, "quantity": "2", "reason": "Customer returned unopened pack",
            "request_key": str(key),
        })
        self.assertEqual(page.status_code, 302)
        ret = PharmacyReturn.objects.get()
        self.assertEqual((ret.quantity, ret.status), (2, PharmacyReturn.Status.QUARANTINED))
        batch.refresh_from_db()
        self.assertEqual((batch.on_hand, batch.quarantined), (6, 2))
        self.assertEqual(batch_usable_quantity(batch), 4)
        # Reusing the request key must not quarantine a second time.
        replay = self.client.post(reverse("pharmacy_return_new"), {
            "sale_line": line.pk, "quantity": "2", "reason": "Customer returned unopened pack",
            "request_key": str(key),
        })
        self.assertEqual(replay.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual((batch.quarantined, PharmacyReturn.objects.count()), (2, 1))
        # A sale with recorded returns cannot be cancelled; the register keeps it.
        cancel = self.client.post(reverse("pharmacy_sale_cancel", args=[sale.pk]), {"reason": "Billing error"})
        self.assertEqual(cancel.status_code, 302)
        sale.refresh_from_db()
        self.assertEqual(sale.status, PharmacySale.Status.COMPLETED)

    # ── FEFO and prescriptions ────────────────────────────────────────────

    def test_fefo_skips_quarantined_batch_and_handover_requires_sellable_stock(self):
        near = self.batch(medicine=self.rx_medicine, number="NEAR", days=20)
        far = self.batch(medicine=self.rx_medicine, number="FAR", days=200)
        MedicineBatch.objects.filter(pk=near.pk).update(status=MedicineBatch.Status.QUARANTINED)
        issue = reserve_issue(user=self.pharmacist, consultation=self.consultation,
                              entries=[{"prescription_id": self.rx.pk, "medicine_id": self.rx_medicine.pk,
                                        "quantity": 4}], request_key=uuid4())
        allocation = issue.lines.get().allocations.get()
        self.assertEqual(allocation.batch_id, far.pk)
        with self.assertRaises(ValidationError):
            handover_issue(user=self.pharmacist, issue=issue)
        MedicineBatch.objects.filter(pk=far.pk).update(status=MedicineBatch.Status.RECALLED)
        set_bill_status(user=self.cashier, bill=issue.bill, next_status="paid")
        with self.assertRaises(ValidationError):
            handover_issue(user=self.pharmacist, issue=issue)

    # ── Procurement ───────────────────────────────────────────────────────

    def test_goods_receipt_posts_stock_and_supplier_balance(self):
        supplier = PharmacySupplier.objects.create(name="Beta Distributors")
        key = uuid4()
        purchase = receive_purchase(
            user=self.pharmacist, supplier=supplier, reference="PO-1", invoice_reference="BILL-1",
            lines=[{"medicine_id": self.otc.pk, "batch_number": "PO-BATCH",
                    "expiry_date": timezone.localdate() + timedelta(days=300),
                    "quantity": 30, "purchase_cost": Decimal("4.00"),
                    "selling_price": Decimal("6.00"), "mrp": Decimal("7.00")}],
            request_key=key)
        self.assertEqual(purchase.status, PharmacyPurchase.Status.RECEIVED)
        self.assertEqual(purchase.total, Decimal("120.00"))
        batch = MedicineBatch.objects.get(batch_number="PO-BATCH")
        self.assertEqual(batch.on_hand, 30)
        supplier.refresh_from_db()
        self.assertEqual(supplier.outstanding_balance, Decimal("120.00"))
        repeat = receive_purchase(user=self.pharmacist, supplier=supplier, reference="PO-1",
                                  invoice_reference="BILL-1", lines=[], request_key=key)
        self.assertEqual(repeat.pk, purchase.pk)
        self.assertEqual(MedicineBatch.objects.get(batch_number="PO-BATCH").on_hand, 30)
        with self.assertRaises(ValidationError):
            receive_purchase(user=self.pharmacist, supplier=supplier, reference="PO-2",
                             invoice_reference="BILL-2",
                             lines=[{"medicine_id": self.otc.pk, "batch_number": "BAD",
                                     "expiry_date": timezone.localdate() - timedelta(days=1),
                                     "quantity": 5, "purchase_cost": Decimal("1"),
                                     "selling_price": Decimal("2")}],
                             request_key=uuid4())

    # ── Returns, holds, transfers ─────────────────────────────────────────

    def test_patient_return_is_quarantined_and_cannot_exceed_sale(self):
        batch = self.batch()
        sale = create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                             "batch_id": batch.pk, "quantity": 4}],
                           amount_received="25.20", request_key=uuid4())
        line = sale.lines.get()
        ret = create_patient_return(user=self.pharmacist, line=line, quantity=2,
                                    reason="Adverse reaction", request_key=uuid4())
        self.assertEqual(ret.status, PharmacyReturn.Status.QUARANTINED)
        self.assertEqual(ret.refund, Decimal("12.00"))
        batch.refresh_from_db()
        self.assertEqual((batch.on_hand, batch.quarantined), (18, 2))
        self.assertEqual(batch_usable_quantity(batch), 16)
        with self.assertRaises(ValidationError):
            create_patient_return(user=self.pharmacist, line=line, quantity=3,
                                  reason="Second return", request_key=uuid4())

    def test_holding_stock_in_quarantine_requires_a_reason_and_free_stock(self):
        batch = self.batch(units=10)
        with self.assertRaises(ValidationError):
            hold_quarantine(user=self.pharmacist, batch=batch, quantity=4, reason="",
                            request_key=uuid4())
        hold_quarantine(user=self.pharmacist, batch=batch, quantity=4, reason="Suspected cold-chain break",
                        request_key=uuid4())
        batch.refresh_from_db()
        self.assertEqual(batch.quarantined, 4)
        self.assertEqual(batch_usable_quantity(batch), 6)
        self.assertEqual(StockMovement.objects.filter(kind="quarantine").count(), 1)
        with self.assertRaises(ValidationError):
            hold_quarantine(user=self.pharmacist, batch=batch, quantity=7, reason="Too many",
                            request_key=uuid4())

    def test_quarantine_release_outcomes_and_write_off(self):
        batch = self.batch(units=10)
        MedicineBatch.objects.filter(pk=batch.pk).update(quarantined=4)
        release_quarantine(user=self.pharmacist, batch=batch, quantity=2, reason="Sealed pack",
                           request_key=uuid4(), target="active")
        batch.refresh_from_db()
        self.assertEqual(batch.quarantined, 2)
        self.assertEqual(batch_usable_quantity(batch), 8)
        release_quarantine(user=self.pharmacist, batch=batch, quantity=2, reason="Crushed",
                           request_key=uuid4(), target="writeoff")
        batch.refresh_from_db()
        self.assertEqual((batch.quarantined, batch.on_hand), (0, 8))
        with self.assertRaises(ValidationError):
            release_quarantine(user=self.pharmacist, batch=batch, quantity=1, reason="Nothing held",
                               request_key=uuid4(), target="active")

    def test_damage_hold_reduces_sellable_stock_and_transfer_records_location(self):
        batch = self.batch(units=10)
        record_damage(user=self.pharmacist, batch=batch, quantity=2, reason="Broken seal",
                      request_key=uuid4())
        batch.refresh_from_db()
        self.assertEqual(batch.damaged, 2)
        self.assertEqual(batch_usable_quantity(batch), 8)
        with self.assertRaises(ValidationError):
            record_damage(user=self.pharmacist, batch=batch, quantity=20, reason="Too many",
                          request_key=uuid4())
        transfer_stock(user=self.pharmacist, batch=batch, quantity=3, to_location="Cold room",
                       reason="Temperature control", request_key=uuid4())
        batch.refresh_from_db()
        self.assertEqual(batch.storage_location, "Cold room")
        transfer = StockTransfer.objects.get()
        self.assertEqual((transfer.quantity, transfer.status), (3, "completed"))

    def test_supplier_return_releases_held_units_and_refund_settlement_closes_patient_returns(self):
        batch = self.batch(number="SUP-RET", units=12)
        self.batch(units=6)
        sale = create_sale(user=self.pharmacist,
                           line_specs=[{"medicine_id": self.otc.pk,
                                        "batch_id": MedicineBatch.objects.get(batch_number="B1").pk,
                                        "quantity": 3}],
                           amount_received="18.90", request_key=uuid4())
        line = sale.lines.get()
        ret = create_patient_return(user=self.pharmacist, line=line, quantity=2,
                                    reason="Returned strip", request_key=uuid4())
        self.assertEqual(ret.status, PharmacyReturn.Status.QUARANTINED)
        # A quarantined strip is part of the physical count: on_hand 3, quarantined 2.
        settle_patient_return(user=self.pharmacist, ret=ret, request_key=uuid4())
        ret.refresh_from_db()
        self.assertEqual(ret.status, PharmacyReturn.Status.COMPLETED)
        # Replaying the settlement key must not double-audit.
        before = PharmacyAudit.objects.count()
        settle_patient_return(user=self.pharmacist, ret=ret, request_key=uuid4())
        self.assertEqual(PharmacyAudit.objects.count(), before)
        # Supplier return takes held units first and reduces the physical count.
        supplier_ret = return_to_supplier(user=self.pharmacist, batch=batch, quantity=5,
                                          reason="Expiry recall", request_key=uuid4())
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 7)
        self.assertEqual(supplier_ret.kind, PharmacyReturn.Kind.SUPPLIER)
        self.assertEqual(supplier_ret.refund, Decimal("20.00"))
        # Reserved units can never leave for the supplier.
        rx_batch = self.batch(medicine=self.rx_medicine, number="SUP-RES", units=4)
        reserve_issue(user=self.pharmacist, consultation=self.consultation,
                      entries=[{"prescription_id": self.rx.pk, "medicine_id": self.rx_medicine.pk,
                                "quantity": 3}], request_key=uuid4())
        with self.assertRaises(ValidationError):
            return_to_supplier(user=self.pharmacist, batch=rx_batch, quantity=4,
                               reason="Excess", request_key=uuid4())
        allowed = return_to_supplier(user=self.pharmacist, batch=rx_batch, quantity=1,
                                     reason="One free unit", request_key=uuid4())
        rx_batch.refresh_from_db()
        self.assertEqual((allowed.quantity, rx_batch.on_hand, rx_batch.reserved), (1, 3, 3))

    def test_returns_page_offers_patient_and_supplier_workflows(self):
        batch = self.batch(number="RET-PAGE", units=9)
        self.client.force_login(self.pharmacist)
        page = self.client.get(reverse("pharmacy_returns"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Process a patient return")
        self.assertContains(page, "Return stock to the supplier")
        posted = self.client.post(reverse("pharmacy_supplier_return"), {
            "batch": batch.pk, "quantity": "4", "reason": "Expired cartons",
            "request_key": str(uuid4()),
        })
        self.assertEqual(posted.status_code, 302)
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 5)
        self.assertEqual(PharmacyReturn.objects.filter(kind=PharmacyReturn.Kind.SUPPLIER).count(), 1)
        # The register filters by type and shows the new supplier return.
        supplier_only = self.client.get(reverse("pharmacy_returns"), {"kind": "supplier"})
        self.assertEqual(supplier_only.context["counts"]["supplier"], 1)
        self.assertContains(supplier_only, "Expired cartons")

    def test_supplier_payment_guard_prevents_overpayment(self):
        supplier = PharmacySupplier.objects.create(name="Epsilon Labs")
        purchase = receive_purchase(
            user=self.pharmacist, supplier=supplier, reference="PO-PAY", invoice_reference="INV-PAY",
            lines=[{"medicine_id": self.otc.pk, "batch_number": "PAY-BATCH",
                    "expiry_date": timezone.localdate() + timedelta(days=200),
                    "quantity": 10, "purchase_cost": Decimal("5.00"),
                    "selling_price": Decimal("7.00")}],
            request_key=uuid4())
        self.client.force_login(self.admin)
        page = self.client.get(reverse("pharmacy_purchases"))
        self.assertEqual(page.context["total_count"], 1)
        self.assertEqual(page.context["received_count"], 1)
        over = self.client.post(reverse("pharmacy_purchase_pay", args=[purchase.pk]), {"amount": "60.00"})
        self.assertEqual(over.status_code, 302)
        purchase.refresh_from_db()
        supplier.refresh_from_db()
        self.assertEqual(purchase.paid, Decimal("0.00"))
        self.assertEqual(supplier.outstanding_balance, Decimal("50.00"))
        ok = self.client.post(reverse("pharmacy_purchase_pay", args=[purchase.pk]), {"amount": "20.00"})
        self.assertEqual(ok.status_code, 302)
        purchase.refresh_from_db()
        supplier.refresh_from_db()
        self.assertEqual(purchase.paid, Decimal("20.00"))
        self.assertEqual(purchase.outstanding, Decimal("30.00"))
        self.assertEqual(supplier.outstanding_balance, Decimal("30.00"))

    # ── Dashboard, reports and permissions ────────────────────────────────

    def test_dashboard_metrics_reflect_real_activity(self):
        batch = self.batch(units=10)
        self.batch(medicine=self.rx_medicine, number="RX-DASH", units=10)
        create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk,
                                                      "batch_id": batch.pk, "quantity": 2}],
                    amount_received="12.60", request_key=uuid4())
        issue = reserve_issue(user=self.pharmacist, consultation=self.consultation,
                              entries=[{"prescription_id": self.rx.pk, "medicine_id": self.rx_medicine.pk,
                                        "quantity": 2}], request_key=uuid4())
        set_bill_status(user=self.cashier, bill=issue.bill, next_status="paid")
        handover_issue(user=self.pharmacist, issue=issue)
        self.client.force_login(self.pharmacist)
        context = self.client.get(reverse("pharmacy_dashboard")).context
        metrics = context["metrics"]
        self.assertEqual(metrics["gross"], Decimal("12.60") + issue.bill.amount)
        self.assertEqual(metrics["net"], metrics["gross"] - metrics["discounts"])
        self.assertEqual(metrics["collections"], Decimal("12.60") + issue.bill.amount_due)
        self.assertEqual(metrics["pending"], 1)
        self.assertEqual(metrics["partial"], 1)
        self.assertEqual(metrics["completed"], 0)
        self.assertEqual(metrics["active_medicines"], 2)
        expiry = self.client.get(reverse("pharmacy_expiry"), {"window": "warning"})
        self.assertContains(expiry, "Expiry management")

    def test_new_pharmacy_routes_require_pharmacy_access(self):
        pharmacy_routes = ("pharmacy_pos", "pharmacy_expiry", "pharmacy_returns", "pharmacy_sales_history")
        admin_routes = ("pharmacy_adjust", "pharmacy_suppliers", "pharmacy_purchases",
                        "pharmacy_reports", "pharmacy_settings", "pharmacy_audit")
        self.client.force_login(self.service_user)
        for name in pharmacy_routes + admin_routes:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.client.force_login(self.pharmacist)
        for name in pharmacy_routes:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        for name in admin_routes:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)
        self.client.force_login(self.admin)
        for name in pharmacy_routes + admin_routes:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_pos_add_and_checkout_through_the_web_flow(self):
        batch = self.batch(units=5)
        self.client.force_login(self.pharmacist)
        add = self.client.post(reverse("pharmacy_pos_add"), {"batch": batch.pk, "quantity": 2})
        self.assertEqual(add.status_code, 302)
        page = self.client.get(reverse("pharmacy_pos"))
        self.assertEqual(page.context["total"], Decimal("12.60"))
        key = uuid4()
        result = self.client.post(reverse("pharmacy_pos_checkout"), {
            "request_key": str(key), "payment_method": "cash", "amount_received": "12.60",
            "walk_in_name": "Walk in", "discount": "0",
        })
        self.assertEqual(result.status_code, 302)
        sale = PharmacySale.objects.get()
        self.assertEqual(sale.lines.get().quantity, 2)
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 3)
        self.assertEqual(self.client.session.get("pharmacy_sale_cart", None), [])
        # A prescription-only item cannot be added from the POS.
        rx_batch = self.batch(medicine=self.rx_medicine, number="RX-POS")
        blocked = self.client.post(reverse("pharmacy_pos_add"), {"batch": rx_batch.pk, "quantity": 1})
        self.assertEqual(blocked.status_code, 302)
        self.assertEqual(self.client.session.get("pharmacy_sale_cart", []), [])

    def test_pos_add_refuses_to_exceed_batch_stock_in_the_basket(self):
        batch = self.batch(units=5)
        self.client.force_login(self.pharmacist)
        self.client.post(reverse("pharmacy_pos_add"), {"batch": batch.pk, "quantity": 4})
        self.client.post(reverse("pharmacy_pos_add"), {"batch": batch.pk, "quantity": 4})
        cart = self.client.session["pharmacy_sale_cart"]
        self.assertEqual(cart, [{"batch_id": batch.pk, "quantity": 4, "discount": "0"}])
        self.assertEqual(self.client.get(reverse("pharmacy_pos")).context["total"], Decimal("25.20"))
        # Checkout still revalidates against live stock, not the stored basket.
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand, 5)

    def test_pos_basket_tax_matches_checkout_when_medicine_has_no_gst_rate(self):
        # Fallback GST from Pharmacy settings must be applied identically in the
        # basket preview and the committed sale.
        # 0 on the medicine means "not configured", so the Pharmacy default applies.
        Medicine.objects.filter(pk=self.otc.pk).update(gst_rate=Decimal("0"))
        settings = PharmacySettings.current()
        settings.default_gst_rate = Decimal("12.00")
        settings.save()
        batch = self.batch(units=5, price="10.00")
        self.client.force_login(self.pharmacist)
        self.client.post(reverse("pharmacy_pos_add"), {"batch": batch.pk, "quantity": 2})
        preview = self.client.get(reverse("pharmacy_pos")).context
        self.assertEqual(preview["tax"], Decimal("2.40"))
        self.assertEqual(preview["total"], Decimal("22.40"))
        sale = self.client.post(reverse("pharmacy_pos_checkout"), {
            "request_key": str(uuid4()), "payment_method": "cash", "amount_received": "22.40",
            "walk_in_name": "Walk in", "discount": "0",
        })
        self.assertEqual(sale.status_code, 302)
        stored = PharmacySale.objects.get()
        self.assertEqual(stored.tax, Decimal("2.40"))
        self.assertEqual(stored.total, Decimal("22.40"))

    def test_pos_update_cannot_exceed_sellable_stock(self):
        batch = self.batch(units=5)
        self.client.force_login(self.pharmacist)
        self.client.post(reverse("pharmacy_pos_add"), {"batch": batch.pk, "quantity": 2})
        over = self.client.post(reverse("pharmacy_pos_update"),
                                {"batch": batch.pk, "quantity": 9, "discount": "0"})
        self.assertEqual(over.status_code, 302)
        self.assertEqual(self.client.session["pharmacy_sale_cart"][0]["quantity"], 2)
        ok = self.client.post(reverse("pharmacy_pos_update"),
                              {"batch": batch.pk, "quantity": 4, "discount": "1.00"})
        self.assertEqual(ok.status_code, 302)
        cart = self.client.session["pharmacy_sale_cart"][0]
        self.assertEqual((cart["quantity"], cart["discount"]), (4, "1.00"))

    def test_goods_receipt_page_posts_multiple_lines(self):
        supplier = PharmacySupplier.objects.create(name="Delta Pharma")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("pharmacy_purchase_new"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Add another line")
        key = uuid4()
        data = {
            "supplier": supplier.pk, "reference": "PO-MULTI", "invoice_reference": "INV-MULTI",
            "request_key": str(key), "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
            "lines-0-medicine": self.otc.pk, "lines-0-batch_number": "MULTI-A",
            "lines-0-expiry_date": (timezone.localdate() + timedelta(days=180)).isoformat(),
            "lines-0-quantity": "10", "lines-0-purchase_cost": "4.00",
            "lines-0-selling_price": "6.00", "lines-0-mrp": "7.00",
            "lines-1-medicine": self.rx_medicine.pk, "lines-1-batch_number": "MULTI-B",
            "lines-1-expiry_date": (timezone.localdate() + timedelta(days=240)).isoformat(),
            "lines-1-quantity": "5", "lines-1-purchase_cost": "2.00",
            "lines-1-selling_price": "3.00",
        }
        response = self.client.post(reverse("pharmacy_purchase_new"), data)
        self.assertEqual(response.status_code, 302)
        purchase = PharmacyPurchase.objects.get(reference="PO-MULTI")
        self.assertEqual(purchase.lines.count(), 2)
        self.assertEqual(MedicineBatch.objects.get(batch_number="MULTI-A").on_hand, 10)
        self.assertEqual(MedicineBatch.objects.get(batch_number="MULTI-A").mrp, Decimal("7.00"))
        self.assertEqual(MedicineBatch.objects.get(batch_number="MULTI-B").on_hand, 5)
        supplier.refresh_from_db()
        self.assertEqual(supplier.outstanding_balance, Decimal("50.00"))

    def test_every_pharmacy_workflow_page_renders(self):
        batch = self.batch(units=10)
        supplier = PharmacySupplier.objects.create(name="Gamma Supplies")
        purchase = receive_purchase(
            user=self.pharmacist, supplier=supplier, reference="PO-RENDER",
            invoice_reference="INV-RENDER",
            lines=[{"medicine_id": self.otc.pk, "batch_number": "RENDER-BATCH",
                    "expiry_date": timezone.localdate() + timedelta(days=200),
                    "quantity": 5, "purchase_cost": Decimal("2"), "selling_price": Decimal("3")}],
            request_key=uuid4())
        sale = create_sale(user=self.pharmacist,
                           line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk, "quantity": 1}],
                           amount_received="6.30", request_key=uuid4())
        self.client.force_login(self.admin)
        pages = [
            reverse("pharmacy_sale_invoice", args=[sale.pk]),
            reverse("pharmacy_batch_detail", args=[batch.pk]),
            reverse("pharmacy_purchase_detail", args=[purchase.pk]),
            reverse("pharmacy_supplier_new"),
            reverse("pharmacy_supplier_edit", args=[supplier.pk]),
            reverse("pharmacy_purchase_new"),
            reverse("pharmacy_batch_action") + "?action=damage",
            reverse("pharmacy_batch_action") + "?action=release",
            reverse("pharmacy_batch_action") + "?action=transfer",
            reverse("pharmacy_pos") + "?q=Paracetamol",
        ]
        for url in pages:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)

    def test_catalogue_form_saves_pharmacy_master_fields(self):
        self.client.force_login(self.pharmacist)
        page = self.client.get(reverse("medicine_catalogue"))
        for label in ("Composition", "Dispensing rules", "Drug schedule", "HSN code",
                      "Rack location", "Storage instructions"):
            self.assertContains(page, label)
        saved = self.client.post(reverse("medicine_catalogue"), {
            "name": "Metformin 500", "generic_name": "Metformin", "strength": "500 mg",
            "form": "Tablet", "unit": "tablet", "pack_size": "15", "reorder_level": "30",
            "is_active": "on", "manufacturer": "USV", "barcode": "8901234567890",
            "composition": "Metformin hydrochloride", "category": "Antidiabetic",
            "hsn_code": "30049099", "drug_schedule": "H", "prescription_required": "on",
            "rack_location": "Rack C2", "storage_instructions": "Store below 30C",
        })
        self.assertEqual(saved.status_code, 302)
        medicine = Medicine.objects.get(name="Metformin 500")
        self.assertEqual(medicine.gst_rate, Decimal("0"))
        self.assertEqual(medicine.rack_location, "Rack C2")
        self.assertTrue(medicine.prescription_required)
        self.assertTrue(medicine.is_controlled is False)
        # The new row is visible in the catalogue list it redirects to.
        listing = self.client.get(saved["Location"])
        self.assertContains(listing, "Metformin 500")

    def test_settings_change_expiry_window(self):
        batch = self.batch(number="WINDOW", days=70, units=5)
        settings = PharmacySettings.current()
        settings.expiry_warning_days = 30
        settings.save()
        self.assertFalse(MedicineBatch.objects.filter(pk=batch.pk, expiry_date__lte=timezone.localdate() + timedelta(days=30)).exists())
        settings.expiry_warning_days = 90
        settings.save()
        self.assertTrue(MedicineBatch.objects.filter(pk=batch.pk, expiry_date__lte=timezone.localdate() + timedelta(days=90)).exists())

    def test_expiry_windows_follow_both_configured_thresholds(self):
        # 20 days out lands in both windows; 70 days out only in the wider one.
        near = self.batch(number="NEAR-WIN", days=20, units=4)
        far = self.batch(number="FAR-WIN", days=70, units=4)
        settings = PharmacySettings.current()
        settings.near_expiry_warn_days = 30
        settings.expiry_warning_days = 90
        settings.save()
        self.client.force_login(self.pharmacist)
        critical = self.client.get(reverse("pharmacy_expiry"), {"window": "critical"}).context
        watch = self.client.get(reverse("pharmacy_expiry"), {"window": "watch"}).context
        self.assertEqual([b.pk for b in critical["batches"]], [near.pk])
        self.assertEqual(sorted(b.pk for b in watch["batches"]), sorted([near.pk, far.pk]))
        self.assertEqual(critical["counts"]["critical"], 1)
        self.assertEqual(watch["counts"]["watch"], 2)
        # The dashboard leads with the critical window, not the wider watch list.
        dash = self.client.get(reverse("pharmacy_dashboard")).context
        self.assertEqual(dash["metrics"]["critical"], 1)
        self.assertEqual([a[0] for a in dash["alerts"] if a[0] == "expiring"], ["expiring"])
        self.assertIn("within 30 days", dash["alerts"][0][1])

    # ── Workspace integration ─────────────────────────────────────────────

    def _order(self, supplier, reference="PO-ORDER-1", batch_number="ORD-1", expiry_days=200, quantity=6):
        return record_purchase_order(
            user=self.pharmacist, supplier=supplier, reference=reference, invoice_reference="INV-ORDER",
            lines=[{"medicine_id": self.otc.pk, "batch_number": batch_number,
                    "expiry_date": timezone.localdate() + timedelta(days=expiry_days),
                    "quantity": quantity, "purchase_cost": Decimal("4.00"),
                    "selling_price": Decimal("6.00")}],
            request_key=uuid4(),
        )

    def test_order_only_purchase_defers_stock_and_payables(self):
        supplier = PharmacySupplier.objects.create(name="Order Supplier")
        order = self._order(supplier)
        self.assertEqual(order.status, PharmacyPurchase.Status.ORDERED)
        self.assertFalse(StockMovement.objects.filter(kind="receipt").exists())
        supplier.refresh_from_db()
        self.assertEqual(supplier.outstanding_balance, Decimal("0.00"))
        self.client.force_login(self.admin)
        page = self.client.get(reverse("pharmacy_purchases"), {"status": "ordered"})
        self.assertContains(page, "PO-ORDER-1")
        self.assertEqual(page.context["pending_count"], 1)
        with self.assertRaises(ValidationError):
            record_purchase_order(
                user=self.pharmacist, supplier=supplier, reference="PO-ORDER-1", invoice_reference="",
                lines=[{"medicine_id": self.otc.pk, "batch_number": "ORD-2",
                        "expiry_date": timezone.localdate() + timedelta(days=90),
                        "quantity": 1, "purchase_cost": Decimal("4"), "selling_price": Decimal("6")}],
                request_key=uuid4())

    def test_ordered_purchase_receives_stock_and_supplier_balance_once(self):
        supplier = PharmacySupplier.objects.create(name="Receive Supplier")
        order = self._order(supplier)
        self.client.force_login(self.admin)
        detail = self.client.get(reverse("pharmacy_purchase_detail", args=[order.pk]))
        self.assertContains(detail, "Receive all lines with batch details")
        posted = self.client.post(reverse("pharmacy_purchase_detail", args=[order.pk]), {"action": "receive"})
        self.assertEqual(posted.status_code, 302)
        order.refresh_from_db()
        supplier.refresh_from_db()
        self.assertEqual(order.status, PharmacyPurchase.Status.RECEIVED)
        self.assertEqual(supplier.outstanding_balance, Decimal("24.00"))
        batch = MedicineBatch.objects.get(batch_number="ORD-1")
        self.assertEqual(batch.on_hand, 6)
        # A second receive has nothing left to post and does not double the balance.
        purchase, value, missing = receive_ordered_purchase(user=self.pharmacist, purchase=order)
        self.assertEqual((value, missing), (0, []))
        supplier.refresh_from_db()
        self.assertEqual(supplier.outstanding_balance, Decimal("24.00"))

    def test_receiving_an_order_requires_batch_details(self):
        supplier = PharmacySupplier.objects.create(name="Incomplete Supplier")
        order = self._order(supplier, batch_number="", expiry_days=200)
        with self.assertRaises(ValidationError):
            receive_ordered_purchase(user=self.pharmacist, purchase=order)
        line = order.lines.get()
        set_purchase_line_details(user=self.pharmacist, line=line, batch_number="LATE-BATCH",
                                  expiry_date=timezone.localdate() + timedelta(days=150))
        purchase, value, missing = receive_ordered_purchase(user=self.pharmacist, purchase=order)
        self.assertEqual((value, missing), (Decimal("24.00"), []))
        self.assertEqual(MedicineBatch.objects.get(batch_number="LATE-BATCH").on_hand, 6)

    def test_sales_history_payment_filter_and_credit_balance(self):
        batch = self.batch(units=8)
        create_sale(user=self.pharmacist, line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk,
                                                      "quantity": 1}], amount_received="6.30",
                    payment_method="cash", request_key=uuid4())
        create_sale(user=self.pharmacist, patient=self.patient,
                    line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk, "quantity": 1}],
                    amount_received="0", payment_method="credit", request_key=uuid4())
        self.client.force_login(self.pharmacist)
        cash = self.client.get(reverse("pharmacy_sales_history"), {"payment": "cash"})
        credit = self.client.get(reverse("pharmacy_sales_history"), {"payment": "credit"})
        self.assertEqual(cash.context["sales"].paginator.count, 1)
        self.assertEqual(credit.context["sales"].paginator.count, 1)
        self.assertEqual(credit.context["counts"]["credit_balance"], Decimal("6.30"))

    def test_returns_prefill_from_a_sale_and_a_batch(self):
        batch = self.batch(number="RET-PREFILL", units=8)
        sale = create_sale(user=self.pharmacist,
                           line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk, "quantity": 3}],
                           amount_received="18.90", request_key=uuid4())
        line = sale.lines.get()
        self.client.force_login(self.pharmacist)
        page = self.client.get(reverse("pharmacy_returns"), {"sale": sale.pk})
        self.assertEqual(list(page.context["form"].fields["sale_line"].queryset), [line])
        self.assertEqual(page.context["form"].initial["sale_line"], line.pk)
        self.assertContains(page, "Returning against")
        label = page.context["form"].fields["sale_line"].label_from_instance(line)
        self.assertIn(sale.reference, label)
        self.assertIn(self.otc.name, label)
        by_reference = self.client.get(reverse("pharmacy_returns"), {"sale": sale.reference})
        self.assertEqual(by_reference.context["selected_sale"], sale)
        batch_page = self.client.get(reverse("pharmacy_returns"), {"batch": batch.pk})
        self.assertEqual(batch_page.context["supplier_form"].initial["batch"], batch.pk)
        self.assertContains(batch_page, "Returning batch RET-PREFILL")

    def test_stock_ledger_filters_and_source_links(self):
        batch = self.batch(units=5)
        sale = create_sale(user=self.pharmacist,
                           line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk, "quantity": 2}],
                           amount_received="12.60", request_key=uuid4())
        self.client.force_login(self.pharmacist)
        page = self.client.get(reverse("pharmacy_stock_report"), {"movement_kind": "sale"})
        self.assertEqual({movement.kind for movement in page.context["movements"]}, {"sale"})
        self.assertContains(page, reverse("pharmacy_sale_invoice", args=[sale.pk]))
        receipt_only = self.client.get(reverse("pharmacy_stock_report"), {"movement_kind": "receipt"})
        self.assertEqual({movement.kind for movement in receipt_only.context["movements"]}, {"receipt"})

    def test_audit_entity_links_resolve(self):
        batch = self.batch(units=4)
        sale = create_sale(user=self.pharmacist,
                           line_specs=[{"medicine_id": self.otc.pk, "batch_id": batch.pk, "quantity": 1}],
                           amount_received="6.30", request_key=uuid4())
        self.client.force_login(self.admin)
        page = self.client.get(reverse("pharmacy_audit"), {"q": sale.reference})
        self.assertContains(page, reverse("pharmacy_sale_invoice", args=[sale.pk]))

    def test_pos_explains_prescription_only_matches(self):
        self.client.force_login(self.pharmacist)
        page = self.client.get(reverse("pharmacy_pos"), {"q": "Amoxicillin"})
        self.assertEqual(page.context["rx_matches"], 1)
        self.assertContains(page, "prescription-only medicine")
        self.assertContains(page, reverse("pharmacy_queue"))

    def test_dashboard_capsules_match_queue_counts(self):
        self.batch(medicine=self.rx_medicine, number="CAP-RX", units=10)
        issue = reserve_issue(
            user=self.pharmacist, consultation=self.consultation,
            entries=[{"prescription_id": self.rx.pk, "medicine_id": self.rx_medicine.pk, "quantity": 2}],
            request_key=uuid4())
        set_bill_status(user=self.cashier, bill=issue.bill, next_status="paid")
        self.client.force_login(self.pharmacist)
        dash = self.client.get(reverse("pharmacy_dashboard"))
        queue = self.client.get(reverse("pharmacy_queue"), {"tab": "ready"})
        vitals = {cap["key"]: cap["value"] for cap in dash.context["vitals"] if cap["key"]}
        tabs = {key: count for key, _, count in queue.context["tabs"]}
        self.assertEqual(vitals["ready"], tabs["ready"])
        self.assertEqual(dash.context["queue_counts"]["ready"], tabs["ready"])
        self.assertContains(dash, "Ready to hand over")

    def test_settings_rejects_a_critical_window_wider_than_the_watch_window(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("pharmacy_settings"), {
            "expiry_warning_days": "30", "near_expiry_warn_days": "90",
            "default_gst_rate": "0", "max_discount_percent": "10",
            "allow_direct_sales": "on", "require_controlled_authorisation": "on",
        })
        self.assertEqual(response.status_code, 200)
        settings = PharmacySettings.current()
        self.assertEqual(settings.expiry_warning_days, 90)
        self.assertContains(response, "critical window must be shorter")

    def test_partial_receipt_caps_supplier_payment_at_received_value(self):
        supplier = PharmacySupplier.objects.create(name="Partial Supplier")
        order = record_purchase_order(
            user=self.pharmacist, supplier=supplier, reference="PO-PARTIAL-1",
            invoice_reference="INV-PARTIAL",
            lines=[
                {"medicine_id": self.otc.pk, "batch_number": "PART-A",
                 "expiry_date": timezone.localdate() + timedelta(days=200),
                 "quantity": 6, "purchase_cost": Decimal("4.00"),
                 "selling_price": Decimal("6.00")},
                {"medicine_id": self.otc.pk, "batch_number": "", "expiry_date": None,
                 "quantity": 6, "purchase_cost": Decimal("4.00"),
                 "selling_price": Decimal("6.00")},
            ],
            request_key=uuid4())
        purchase, value, missing = receive_ordered_purchase(user=self.pharmacist, purchase=order)
        self.assertEqual(value, Decimal("24.00"))
        self.assertEqual(len(missing), 1)
        purchase.refresh_from_db()
        supplier.refresh_from_db()
        self.assertEqual(purchase.status, PharmacyPurchase.Status.PARTIAL)
        self.assertEqual(supplier.outstanding_balance, Decimal("24.00"))
        self.client.force_login(self.admin)
        page = self.client.get(reverse("pharmacy_purchase_detail", args=[order.pk]))
        self.assertEqual(page.context["payable"], Decimal("24.00"))
        self.assertContains(page, "Pay only for goods received")
        over = self.client.post(reverse("pharmacy_purchase_pay", args=[order.pk]), {"amount": "30.00"})
        self.assertEqual(over.status_code, 302)
        purchase.refresh_from_db()
        supplier.refresh_from_db()
        self.assertEqual(purchase.paid, Decimal("0.00"))
        self.assertEqual(supplier.outstanding_balance, Decimal("24.00"))
        ok = self.client.post(reverse("pharmacy_purchase_pay", args=[order.pk]), {"amount": "24.00"})
        self.assertEqual(ok.status_code, 302)
        purchase.refresh_from_db()
        supplier.refresh_from_db()
        self.assertEqual(purchase.paid, Decimal("24.00"))
        self.assertEqual(supplier.outstanding_balance, Decimal("0.00"))
