"""Transactional pharmacy and department operations shared by every entry point."""
from decimal import Decimal
from uuid import UUID, uuid4
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from patients.models import (
    Medicine, MedicineBatch, StockMovement, PrescriptionItem, PharmacyIssue,
    PharmacyIssueLine, BatchAllocation, PatientBill, OPDAuditEvent, ClinicalOrder,
    PharmacySettings, PharmacySupplier, PharmacyPurchase, PharmacyPurchaseLine,
    PharmacySale, PharmacySaleLine, PharmacyReturn, StockTransfer, PharmacyAudit,
)


def designation(user):
    return getattr(getattr(user, "profile", None), "designation", "")


def is_admin(user):
    return user.is_superuser or designation(user) == "admin"


def require_pharmacy(user):
    if not is_admin(user) and designation(user) != "pharmacy":
        raise PermissionDenied("Pharmacy access required.")


def require_admin(user):
    if not is_admin(user):
        raise PermissionDenied("Administrator access required.")


def scoped_orders(user):
    qs = ClinicalOrder.objects.all()
    if is_admin(user):
        return qs
    role = designation(user)
    if role == "service_staff":
        return qs.filter(department__in=user.profile.service_departments.all())
    if role == "doctor":
        return qs.filter(consultation__doctor=user)
    if role == "opd_assistant":
        return qs
    return qs.none()


def event(user, patient, action, summary, **details):
    return OPDAuditEvent.objects.create(actor=user, patient=patient, action=action, summary=summary[:255], details=details)


def pharmacy_audit(user, action, summary, *, entity="", entity_id="", before=None, after=None, reason="", request_key=None):
    return PharmacyAudit.objects.create(
        actor=user, action=action, entity=entity, entity_id=str(entity_id or "")[:40],
        summary=summary[:255], before=before or {}, after=after or {}, reason=reason[:255],
        request_key=request_key,
    )


def usable_batches(medicine):
    """FEFO-eligible batches: active, in date, and not fully held back."""
    return (
        MedicineBatch.objects.filter(
            medicine=medicine,
            status=MedicineBatch.Status.ACTIVE,
            expiry_date__gte=timezone.localdate(),
            on_hand__gt=F("reserved") + F("damaged") + F("quarantined"),
        ).order_by("expiry_date", "id")
    )


def batch_usable_quantity(batch):
    if batch.status != MedicineBatch.Status.ACTIVE or batch.expiry_date < timezone.localdate():
        return 0
    return max(batch.on_hand - batch.reserved - batch.damaged - batch.quarantined, 0)


PRE_VISIT_SOURCES = (
    PatientBill.Source.REGISTRATION,
    PatientBill.Source.APPOINTMENT,
)


def appointment_pre_visit_bills(appointment):
    if appointment is None:
        return PatientBill.objects.none()
    return appointment.bills.filter(source__in=PRE_VISIT_SOURCES)


def appointment_pre_visit_pending(appointment):
    return appointment_pre_visit_bills(appointment).filter(
        status=PatientBill.Status.PENDING
    ).exists()


def appointment_pre_visit_paid(appointment):
    return not appointment_pre_visit_pending(appointment)


def consultation_pending_bills(consultation):
    if consultation is None or not consultation.pk:
        return PatientBill.objects.none()
    return consultation.bills.filter(status=PatientBill.Status.PENDING)


def consultation_pending_total(consultation):
    return sum(
        (bill.amount_due for bill in consultation_pending_bills(consultation)),
        Decimal("0"),
    )


@transaction.atomic
def sync_service_bills(*, user, consultation):
    """Create/cancel service bills for the orders on a consultation.

    A service order is billed from its price snapshot (or the catalogue price
    when the snapshot is zero). Orders without a price are treated as
    no-charge and remain visible to the service desk immediately.
    """
    created = []
    orders = list(
        consultation.orders.select_related("service_definition", "bill")
    )
    for order in orders:
        if order.status == "Cancelled":
            if order.bill_id and order.bill.status == PatientBill.Status.PENDING:
                PatientBill.objects.filter(
                    pk=order.bill_id, status=PatientBill.Status.PENDING
                ).update(status=PatientBill.Status.CANCELLED)
                event(
                    user,
                    consultation.patient,
                    "service_bill_cancelled",
                    f"Service bill cancelled: {order.name}",
                    order_id=order.pk,
                    bill_id=order.bill_id,
                )
            continue

        # A re-added order may still point at an old cancelled bill.
        if order.bill_id and order.bill.status == PatientBill.Status.CANCELLED:
            ClinicalOrder.objects.filter(pk=order.pk).update(bill=None)
            order.bill = None

        if order.bill_id:
            continue

        price = order.price or (
            order.service_definition.price if order.service_definition_id else 0
        )
        if not price or price <= 0:
            continue

        bill = PatientBill.objects.create(
            patient=consultation.patient,
            source=PatientBill.Source.SERVICE,
            consultation=consultation,
            description=f"{order.name} · {consultation.visit_token}"[:200],
            amount=price,
            created_by=user,
        )
        ClinicalOrder.objects.filter(pk=order.pk).update(bill=bill)
        order.bill = bill
        created.append(bill)
        event(
            user,
            consultation.patient,
            "service_bill_created",
            f"{order.name} · ₹{bill.amount_due}",
            order_id=order.pk,
            bill_id=bill.pk,
        )
    return created


@transaction.atomic
def pay_bills(*, user, bills, payment_method="", payment_reference=""):
    """Collect payment for one or more bills in a single cashier transaction."""
    bills = list(bills)
    role = designation(user)
    if not is_admin(user) and role != "registration_desk" and not (
        role == "pharmacy" and all(bill.source == PatientBill.Source.PHARMACY for bill in bills)
    ):
        raise PermissionDenied
    bill_ids = [bill.pk for bill in bills if getattr(bill, "pk", None)]
    if not bill_ids:
        raise ValidationError("No bills selected for payment.")
    group = uuid4()
    paid = []
    for bill_id in bill_ids:
        bill = (
            PatientBill.objects.select_for_update()
            .select_related("patient")
            .filter(pk=bill_id)
            .first()
        )
        if bill is None:
            raise ValidationError("A selected bill no longer exists.")
        if bill.status == PatientBill.Status.CANCELLED:
            raise ValidationError(f"{bill.bill_number} was cancelled and cannot be paid.")
        if bill.status == PatientBill.Status.PAID:
            continue

        issue = PharmacyIssue.objects.filter(bill=bill).first()
        if issue:
            if issue.status == PharmacyIssue.Status.CANCELLED:
                raise ValidationError(
                    f"{bill.bill_number}: the pharmacy issue was cancelled."
                )
            if issue.status == PharmacyIssue.Status.DISPENSED:
                raise ValidationError(
                    f"{bill.bill_number}: medicines were already handed over."
                )

        service_orders = list(bill.service_orders.all())
        if service_orders and all(
            order.status == "Cancelled" for order in service_orders
        ):
            raise ValidationError(
                f"{bill.bill_number}: every service on this bill was cancelled."
            )

        method = payment_method or bill.patient.payment_type or "cash"
        if method not in PatientBill.PaymentMethod.values:
            method = PatientBill.PaymentMethod.CASH

        changed = PatientBill.objects.filter(
            pk=bill.pk, status=PatientBill.Status.PENDING
        ).update(
            status=PatientBill.Status.PAID,
            paid_at=timezone.now(),
            paid_by=user,
            payment_method=method,
            payment_reference=(payment_reference or "")[:100],
            payment_group=group,
        )
        if not changed:
            raise ValidationError(
                f"{bill.bill_number} changed while collecting payment. Refresh and retry."
            )
        bill.status = PatientBill.Status.PAID
        paid.append(bill)
        event(
            user,
            bill.patient,
            "bill_paid",
            f"{bill.bill_number} paid · ₹{bill.amount_due}",
            bill_id=bill.pk,
            source=bill.source,
            payment_group=str(group),
            payment_method=method,
        )
        if bill.source == PatientBill.Source.SERVICE:
            event(
                user,
                bill.patient,
                "service_released",
                f"Service bill {bill.bill_number} paid; service orders released",
                bill_id=bill.pk,
            )
        if bill.source in PRE_VISIT_SOURCES and bill.appointment_id:
            event(
                user,
                bill.patient,
                "visit_ready_for_opd",
                f"{bill.bill_number} paid; visit ready for OPD",
                bill_id=bill.pk,
                appointment_id=bill.appointment_id,
            )
    return group, paid


def request_uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Reload this form before submitting again.")


@transaction.atomic
def receive_stock(*, user, medicine, batch_number, expiry_date, supplier, invoice_reference, purchase_cost, selling_price, request_key, packs=None, quantity_units=None, mrp=None):
    require_pharmacy(user)
    key = request_uuid(request_key)
    previous = StockMovement.objects.filter(request_key=key).first()
    if previous:
        return previous.batch
    quantity = quantity_units if quantity_units is not None else (packs or 0) * medicine.pack_size
    if (packs is not None and quantity_units is not None) or not isinstance(quantity, int) or not 1 <= quantity <= 2147483647:
        raise ValidationError("Enter a positive whole quantity in packs or dispensing units.")
    if not medicine.is_active or medicine.pack_size < 1 or expiry_date < timezone.localdate():
        raise ValidationError("Choose an active medicine, positive quantity, and a batch that has not expired.")
    if min(purchase_cost, selling_price) < 0:
        raise ValidationError("Prices cannot be negative.")
    batch, created = MedicineBatch.objects.get_or_create(medicine=medicine, batch_number=batch_number, defaults={
        "expiry_date": expiry_date, "supplier": supplier, "invoice_reference": invoice_reference,
        "purchase_cost": purchase_cost, "selling_price": selling_price, "mrp": mrp,
    })
    if not created and (batch.expiry_date != expiry_date or batch.purchase_cost != purchase_cost or batch.selling_price != selling_price):
        raise ValidationError("This batch already exists with different expiry or prices. Check the receipt.")
    MedicineBatch.objects.filter(pk=batch.pk).update(on_hand=F("on_hand") + quantity)
    StockMovement.objects.create(batch=batch, quantity=quantity, kind="receipt", reason=f"Receipt {invoice_reference} · {supplier}"[:255], actor=user, request_key=key)
    return batch


@transaction.atomic
def update_batch_prices(*, user, batch, purchase_cost, selling_price, mrp, reason, request_key):
    require_pharmacy(user)
    key = request_uuid(request_key)
    if PharmacyAudit.objects.filter(request_key=key).exists():
        return batch
    if not reason.strip():
        raise ValidationError("Record why the prices are changing.")
    if min(purchase_cost, selling_price) < 0 or (mrp is not None and mrp < 0):
        raise ValidationError("Prices cannot be negative.")
    batch = MedicineBatch.objects.select_for_update().get(pk=batch.pk)
    before = {"purchase_cost": str(batch.purchase_cost), "selling_price": str(batch.selling_price), "mrp": str(batch.mrp) if batch.mrp is not None else None}
    MedicineBatch.objects.filter(pk=batch.pk).update(purchase_cost=purchase_cost, selling_price=selling_price, mrp=mrp)
    after = {"purchase_cost": str(purchase_cost), "selling_price": str(selling_price), "mrp": str(mrp) if mrp is not None else None}
    pharmacy_audit(user, "batch_prices_changed", f"Prices changed for {batch}", entity="MedicineBatch", entity_id=batch.pk,
                   before=before, after=after, reason=reason, request_key=key)
    return batch


@transaction.atomic
def adjust_stock(*, user, batch, quantity, reason, request_key):
    require_pharmacy(user)
    key = request_uuid(request_key)
    if StockMovement.objects.filter(request_key=key).exists():
        return
    if not quantity or not reason.strip():
        raise ValidationError("Enter a non-zero correction and its reason.")
    # Conditional writes enforce the invariant on SQLite as well as PostgreSQL.
    if not MedicineBatch.objects.filter(pk=batch.pk, on_hand__gte=F("reserved") - quantity).update(on_hand=F("on_hand") + quantity):
        raise ValidationError("The adjustment would remove reserved stock or make stock negative.")
    StockMovement.objects.create(batch=batch, quantity=quantity, kind="adjustment", reason=reason[:255], actor=user, request_key=key)


@transaction.atomic
def reserve_issue(*, user, consultation, entries, request_key):
    require_pharmacy(user)
    key = request_uuid(request_key)
    existing = PharmacyIssue.objects.filter(request_key=key).first()
    if existing:
        if existing.consultation_id != consultation.pk:
            raise ValidationError("This submission belongs to another prescription.")
        return existing
    if not entries or consultation.status == "cancelled":
        raise ValidationError("Select at least one released medicine to prepare.")
    # Start with a write, serializing the SQLite transaction before reading balances.
    bill = PatientBill.objects.create(
        patient=consultation.patient,
        source=PatientBill.Source.PHARMACY,
        consultation=consultation,
        description=f"Pharmacy · {consultation.visit_token}",
        amount=0,
        created_by=user,
    )
    issue = PharmacyIssue.objects.create(consultation=consultation, bill=bill, request_key=key, created_by=user)
    total = Decimal("0")
    seen = set()
    for entry in entries:
        try:
            quantity = int(entry["quantity"])
            if str(quantity) != str(entry["quantity"]):
                raise ValueError
            rx = PrescriptionItem.objects.get(pk=entry["prescription_id"], consultation=consultation)
            medicine = Medicine.objects.get(pk=entry["medicine_id"], is_active=True)
        except (ValueError, TypeError, KeyError, PrescriptionItem.DoesNotExist, Medicine.DoesNotExist):
            raise ValidationError("Choose a valid prescription, medicine, and whole quantity.")
        if rx.pk in seen or quantity < 1 or quantity > 9999:
            raise ValidationError("Each prescription must have one quantity between 1 and 9999.")
        seen.add(rx.pk)
        if not rx.released_at or not rx.prescribed_quantity or rx.dispense_status in ("cancelled", "dispensed"):
            raise ValidationError(f"{rx.medicine_name}: the doctor must release a prescription with an explicit quantity first.")
        if (rx.medicine_id and rx.medicine_id != medicine.pk) or (not rx.medicine_id and " ".join(rx.medicine_name.casefold().split()) != " ".join(medicine.name.casefold().split())):
            raise ValidationError("The selected medicine must match the prescription exactly; ask the doctor to amend it if needed.")
        changed = PrescriptionItem.objects.filter(pk=rx.pk, dispense_status__in=["ordered", "partial", "unavailable"], prescribed_quantity__gte=F("dispensed_quantity") + F("reserved_quantity") + quantity).update(reserved_quantity=F("reserved_quantity") + quantity, medicine=medicine)
        if not changed:
            raise ValidationError(f"{rx.medicine_name}: this quantity exceeds the unreserved prescription balance.")
        remaining = quantity
        for batch in usable_batches(medicine):
            take = min(remaining, batch.on_hand - batch.reserved - batch.damaged - batch.quarantined)
            if not MedicineBatch.objects.filter(pk=batch.pk, on_hand__gte=F("reserved") + F("damaged") + F("quarantined") + take).update(reserved=F("reserved") + take):
                raise ValidationError("Stock changed while preparing this issue. Refresh and try again.")
            # Separate price lines preserve the actual batch selling price.
            line = PharmacyIssueLine.objects.create(issue=issue, prescription=rx, medicine=medicine, quantity=take, unit_price=batch.selling_price, medicine_name=medicine.name)
            BatchAllocation.objects.create(line=line, batch=batch, quantity=take)
            total += line.total
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise ValidationError(f"Not enough available stock for {medicine.name}. Prepare a smaller quantity or receive stock.")
    bill.amount = total
    PatientBill.objects.filter(pk=bill.pk).update(amount=total)
    event(user, consultation.patient, "pharmacy_reserved", f"Pharmacy issue #{issue.pk} prepared", issue_id=issue.pk, bill_id=bill.pk)
    return issue


@transaction.atomic
def cancel_issue(*, user, issue):
    require_pharmacy(user)
    issue = PharmacyIssue.objects.select_related("bill", "consultation__patient").get(pk=issue.pk)
    if issue.status == "cancelled":
        return issue
    if issue.status != "awaiting_payment":
        raise ValidationError("A handed-over issue cannot be cancelled.")
    # Claim the bill before releasing reservations; a simultaneous cashier payment must fail.
    if not PatientBill.objects.filter(pk=issue.bill_id, status="pending").update(status="cancelled"):
        raise ValidationError("Only unpaid issues can be cancelled.")
    if not PharmacyIssue.objects.filter(pk=issue.pk, status="awaiting_payment").update(status="cancelled"):
        raise ValidationError("This issue has already changed.")
    for allocation in BatchAllocation.objects.filter(line__issue=issue).select_related("line"):
        if not MedicineBatch.objects.filter(pk=allocation.batch_id, reserved__gte=allocation.quantity).update(reserved=F("reserved") - allocation.quantity):
            raise ValidationError("The reserved batch balance needs review.")
        PrescriptionItem.objects.filter(pk=allocation.line.prescription_id, reserved_quantity__gte=allocation.quantity).update(reserved_quantity=F("reserved_quantity") - allocation.quantity)
    event(user, issue.consultation.patient, "pharmacy_issue_cancelled", f"Pharmacy issue #{issue.pk} cancelled", issue_id=issue.pk)
    return issue


@transaction.atomic
def handover_issue(*, user, issue):
    require_pharmacy(user)
    issue = PharmacyIssue.objects.select_related("bill", "consultation__patient").get(pk=issue.pk)
    if issue.status == "dispensed":
        return issue
    if issue.bill.status != "paid" or issue.status != "awaiting_payment":
        raise ValidationError("The linked pharmacy bill must be paid before handing over medicines.")
    # Claim bill and issue with conditional writes before changing physical stock.
    if not PatientBill.objects.filter(pk=issue.bill_id, status="paid").update(status="paid"):
        raise ValidationError("The linked payment changed. Refresh this issue.")
    if not PharmacyIssue.objects.filter(pk=issue.pk, status="awaiting_payment").update(status="dispensed", dispensed_by=user, dispensed_at=timezone.now()):
        raise ValidationError("This issue has already changed.")
    for allocation in BatchAllocation.objects.filter(line__issue=issue).select_related("batch", "line"):
        if not allocation.batch.sellable:
            raise ValidationError("A reserved batch is no longer available (expired, recalled, damaged or quarantined). Reallocate a valid batch before handover.")
        changed = MedicineBatch.objects.filter(pk=allocation.batch_id, reserved__gte=allocation.quantity, on_hand__gte=allocation.quantity).update(on_hand=F("on_hand") - allocation.quantity, reserved=F("reserved") - allocation.quantity)
        if not changed:
            raise ValidationError("The reserved batch balance changed. Refresh this issue.")
        rx = PrescriptionItem.objects.get(pk=allocation.line.prescription_id)
        if rx.dispense_status == "cancelled":
            raise ValidationError("This prescription was cancelled. Ask the doctor to review it.")
        PrescriptionItem.objects.filter(pk=rx.pk, reserved_quantity__gte=allocation.quantity).update(dispensed_quantity=F("dispensed_quantity") + allocation.quantity, reserved_quantity=F("reserved_quantity") - allocation.quantity, dispensed_by=user, updated_at=timezone.now())
        StockMovement.objects.create(batch=allocation.batch, quantity=-allocation.quantity, kind="issue", reason=f"Handover #{issue.pk}", actor=user, issue=issue)
    for rx in PrescriptionItem.objects.filter(issue_lines__issue=issue).distinct():
        rx.dispense_status = "dispensed" if rx.dispensed_quantity >= (rx.prescribed_quantity or 0) else "partial"
        PrescriptionItem.objects.filter(pk=rx.pk).update(dispense_status=rx.dispense_status, updated_at=timezone.now())
    event(user, issue.consultation.patient, "pharmacy_dispense_updated", f"Pharmacy issue #{issue.pk} handed over", issue_id=issue.pk, bill_id=issue.bill_id)
    return issue


@transaction.atomic
def cancel_prescription(*, user, prescription):
    if not is_admin(user) and (designation(user) != "doctor" or prescription.consultation.doctor_id != user.pk):
        raise PermissionDenied
    if prescription.reserved_quantity:
        raise ValidationError("Cancel unpaid reservations first. Paid reservations must be handed over before cancelling the remaining balance.")
    if prescription.dispense_status == "dispensed":
        raise ValidationError("This prescription has already been fully dispensed.")
    if not PrescriptionItem.objects.filter(pk=prescription.pk, reserved_quantity=0).exclude(dispense_status="dispensed").update(dispense_status="cancelled"):
        raise ValidationError("The prescription changed. Refresh and try again.")
    event(user, prescription.consultation.patient, "prescription_balance_cancelled", f"Unissued balance cancelled: {prescription.medicine_name}", prescription_id=prescription.pk)


@transaction.atomic
def reallocate_issue(*, user, issue):
    """Replace expired reservations with fresh batches; retain billed unit prices."""
    require_pharmacy(user)
    if not PharmacyIssue.objects.filter(pk=issue.pk, status="awaiting_payment").update(status="awaiting_payment"):
        raise ValidationError("Only an open issue can be reallocated.")
    for line in issue.lines.all():
        for allocation in line.allocations.all():
            MedicineBatch.objects.filter(pk=allocation.batch_id, reserved__gte=allocation.quantity).update(reserved=F("reserved") - allocation.quantity)
        line.allocations.all().delete()
        remaining = line.quantity
        for batch in usable_batches(line.medicine):
            take = min(remaining, batch.on_hand - batch.reserved - batch.damaged - batch.quarantined)
            if not MedicineBatch.objects.filter(pk=batch.pk, on_hand__gte=F("reserved") + F("damaged") + F("quarantined") + take).update(reserved=F("reserved") + take):
                raise ValidationError("Stock changed. Refresh and retry.")
            BatchAllocation.objects.create(line=line, batch=batch, quantity=take)
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise ValidationError("Not enough valid stock to reallocate this issue. Receive stock first.")
    event(user, issue.consultation.patient, "pharmacy_reallocated", f"Batch allocation refreshed for issue #{issue.pk}; billed prices preserved", issue_id=issue.pk)


@transaction.atomic
def set_bill_status(*, user, bill, next_status, payment_method="", payment_reference=""):
    role = designation(user)
    if not is_admin(user) and role != "registration_desk" and not (
        role == "pharmacy" and bill.source == PatientBill.Source.PHARMACY
    ):
        raise PermissionDenied
    if next_status not in ("paid", "pending"):
        raise ValidationError("Choose paid or pending.")
    if next_status == "paid":
        pay_bills(
            user=user,
            bills=[bill],
            payment_method=payment_method,
            payment_reference=payment_reference,
        )
        return
    # Obtain a write lock before checking related handovers on SQLite.
    if not PatientBill.objects.filter(pk=bill.pk).exclude(status="cancelled").update(status=F("status")):
        raise ValidationError("A cancelled bill cannot be paid or reopened.")
    issue = PharmacyIssue.objects.filter(bill=bill).first()
    if issue and issue.status == "dispensed":
        raise ValidationError("Medicines were handed over against this bill; payment cannot be reset.")
    if issue and issue.status == "cancelled":
        raise ValidationError("This pharmacy issue was cancelled.")
    if bill.service_orders.exclude(status__in=("Ordered", "Cancelled")).exists():
        raise ValidationError(
            "Service work has already started; the payment cannot be reset."
        )
    PatientBill.objects.filter(pk=bill.pk).update(
        status=PatientBill.Status.PENDING,
        paid_at=None,
        paid_by=None,
        payment_method="",
        payment_reference="",
        payment_group=None,
    )
    if issue:
        event(user, bill.patient, "pharmacy_payment_updated", f"{bill.bill_number}: pending", issue_id=issue.pk, bill_id=bill.pk)


def prescription_payload(item):
    return {"id": item.pk, "legacy_dispensed_quantity": item.legacy_dispensed_quantity, "medicine_id": item.medicine_id, "medicine_name": item.medicine_name,
            "prescribed_quantity": item.prescribed_quantity, "released_at": item.released_at.isoformat() if item.released_at else None,
            "dose": item.dose, "route": item.route, "frequency": item.frequency,
            "duration": item.duration, "instructions": item.instructions, "dispense_status": item.dispense_status}


def guarded_consultation_save(function):
    from functools import wraps
    from django.http import JsonResponse
    @wraps(function)
    def wrapped(request, *args, **kwargs):
        try:
            with transaction.atomic():
                response = function(request, *args, **kwargs)
                if response.status_code >= 400:
                    transaction.set_rollback(True)
                return response
        except ValidationError as exc:
            return JsonResponse({"ok": False, "error": " ".join(exc.messages)}, status=400)
        except (ValueError, TypeError, AttributeError, KeyError):
            return JsonResponse({"ok": False, "error": "Invalid consultation data."}, status=400)
    return wrapped


def sync_prescriptions(consultation, data, release=False):
    if not isinstance(data, list):
        raise ValidationError("Prescriptions must be a list.")
    existing = list(consultation.prescriptions.all())
    retained = set()
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValidationError("Invalid prescription item.")
        name = str(row.get("medicine_name", "")).strip()[:255]
        if not name:
            continue
        item = None
        if row.get("id"):
            item = next((rx for rx in existing if rx.pk == int(row["id"])), None)
            if not item or item.pk in retained:
                raise ValidationError("Prescription IDs must belong to this consultation and occur once.")
        else:
            # Compatibility for older clients; never name-match released records.
            matches = [rx for rx in existing if rx.pk not in retained and not rx.released_at and rx.dispense_status != "cancelled" and rx.medicine_name.casefold() == name.casefold()]
            item = matches[0] if len(matches) == 1 else None
        quantity = row.get("prescribed_quantity")
        if quantity not in (None, ""):
            if str(quantity) != str(int(quantity)) or not 1 <= int(quantity) <= 9999:
                raise ValidationError("Prescribed quantities must be whole numbers between 1 and 9999.")
            quantity = int(quantity)
        else:
            quantity = None
        medicine_id = int(row["medicine_id"]) if row.get("medicine_id") else None
        if medicine_id:
            medicine = Medicine.objects.filter(pk=medicine_id, is_active=True).first()
            if not medicine or medicine.name != name:
                raise ValidationError("Medicine identity changed. Select the catalogue medicine again.")
        values = {"medicine_name": name, "medicine_id": medicine_id, "prescribed_quantity": quantity,
                  "dose": str(row.get("dose", "")).strip()[:100], "route": str(row.get("route", "Oral")).strip()[:50],
                  "frequency": str(row.get("frequency", "OD")).strip()[:50], "duration": str(row.get("duration", "5 days")).strip()[:50],
                  "instructions": str(row.get("instructions", "")).strip()[:255]}
        if item and (item.released_at or item.legacy_dispensed_quantity or item.dispense_status == "cancelled"):
            if any(getattr(item, field) != value for field, value in values.items()):
                raise ValidationError("Released or historical prescriptions cannot be edited. Add an amendment and cancel the unissued balance.")
            retained.add(item.pk)
            continue
        if not item:
            item = PrescriptionItem(consultation=consultation)
        for field, value in values.items():
            setattr(item, field, value)
        item.order_index = index
        item.save()
        retained.add(item.pk)
    for item in existing:
        if item.pk not in retained and not item.released_at and not item.legacy_dispensed_quantity:
            item.dispense_status = "cancelled"
            item.save(update_fields=["dispense_status", "updated_at"])
    if release:
        candidates = consultation.prescriptions.filter(released_at__isnull=True).exclude(dispense_status="cancelled")
        if not candidates.exists():
            return
        if candidates.filter(prescribed_quantity__isnull=True).exists():
            raise ValidationError("Enter the total quantity in dispensing units for every medicine before sending to pharmacy.")
        candidates.update(released_at=timezone.now())


# ── Direct sales / POS ───────────────────────────────────────────────────────

SALE_PAYMENT_METHODS = (PharmacySale.PaymentMethod.CASH, PharmacySale.PaymentMethod.UPI,
                        PharmacySale.PaymentMethod.CARD, PharmacySale.PaymentMethod.CREDIT)


def _sale_totals(line_specs):
    subtotal = tax = discount = Decimal("0")
    for spec in line_specs:
        subtotal += spec["unit_price"] * spec["quantity"]
        discount += spec["discount"]
        tax += spec["tax"]
    return subtotal, discount, tax


def parse_sale_line(*, medicine, batch, quantity, discount="0", tax_rate=None):
    """Validate one scanned cart line and snapshot its pricing."""
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ValidationError("Enter a whole quantity for every sale line.")
    if quantity < 1 or quantity > 9999:
        raise ValidationError("Sale quantities must be whole numbers between 1 and 9999.")
    if not medicine.is_active:
        raise ValidationError(f"{medicine.name} is inactive and cannot be sold.")
    if batch.medicine_id != medicine.pk:
        raise ValidationError("The selected batch does not belong to this medicine.")
    if not batch.sellable:
        raise ValidationError(f"{medicine.name}: this batch is expired, recalled, damaged or quarantined and cannot be sold.")
    available = batch_usable_quantity(batch)
    if quantity > available:
        raise ValidationError(f"{medicine.name}: only {available} units are available in this batch.")
    if medicine.prescription_required:
        raise ValidationError(f"{medicine.name} requires a prescription. Dispense it through the prescription queue instead of a direct sale.")
    settings = PharmacySettings.current()
    try:
        discount = Decimal(str(discount or "0"))
    except Exception:
        raise ValidationError("Enter a valid discount amount.")
    if discount < 0:
        raise ValidationError("Discount cannot be negative.")
    if discount > quantity * batch.selling_price:
        raise ValidationError("Discount cannot exceed the line value.")
    if tax_rate in (None, ""):
        tax_rate = medicine.gst_rate or settings.default_gst_rate or Decimal("0")
    tax_rate = Decimal(str(tax_rate))
    if tax_rate < 0 or tax_rate > 100:
        raise ValidationError("Tax rate must be between 0 and 100 percent.")
    tax = (quantity * batch.selling_price - discount) * tax_rate / Decimal("100")
    return {
        "medicine": medicine, "batch": batch, "quantity": quantity,
        "unit_price": batch.selling_price, "discount": discount,
        "tax": tax.quantize(Decimal("0.01")), "tax_rate": tax_rate,
    }


@transaction.atomic
def create_sale(*, user, patient=None, walk_in_name="", line_specs, discount="0", amount_received="0",
                payment_method="cash", payment_reference="", controlled_authorisation="", request_key):
    """Create and finalise a direct pharmacy sale in one atomic checkout."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    existing = PharmacySale.objects.filter(request_key=key).first()
    if existing:
        return existing
    settings = PharmacySettings.current()
    if not settings.allow_direct_sales:
        raise ValidationError("Direct pharmacy sales are disabled in Pharmacy settings.")
    if not line_specs:
        raise ValidationError("Add at least one medicine to the sale.")
    if payment_method not in SALE_PAYMENT_METHODS:
        raise ValidationError("Choose a valid payment method.")
    sale = PharmacySale.objects.create(
        patient=patient, walk_in_name=walk_in_name.strip()[:200],
        status=PharmacySale.Status.DRAFT, created_by=user, request_key=key,
    )
    line_values = []
    for spec in line_specs:
        medicine = Medicine.objects.filter(pk=spec.get("medicine_id"), is_active=True).first()
        batch = MedicineBatch.objects.filter(pk=spec.get("batch_id")).first()
        if not medicine or not batch:
            raise ValidationError("Choose a valid medicine and batch for every sale line.")
        line = parse_sale_line(medicine=medicine, batch=batch,
                               quantity=spec.get("quantity"), discount=spec.get("discount", "0"),
                               tax_rate=spec.get("tax_rate"))
        line["medicine"] = medicine
        line["batch"] = batch
        line_values.append(line)
    if any(line["medicine"].is_controlled for line in line_values):
        # Controlled drugs are never sold without a documented pharmacist authorisation.
        if settings.require_controlled_authorisation and not controlled_authorisation.strip():
            raise ValidationError("This sale contains a controlled medicine. Record the pharmacist authorisation.")
    for line in line_values:
        PharmacySaleLine.objects.create(
            sale=sale, medicine=line["medicine"], batch=line["batch"], quantity=line["quantity"],
            unit_price=line["unit_price"], discount=line["discount"], tax=line["tax"],
            medicine_name=line["medicine"].name,
        )
    subtotal, total_discount, total_tax = _sale_totals(line_values)
    bill_discount = Decimal(str(discount or "0"))
    if bill_discount < 0 or bill_discount > subtotal:
        raise ValidationError("Bill discount cannot be negative or exceed the subtotal.")
    max_discount = subtotal * (settings.max_discount_percent or Decimal("0")) / Decimal("100")
    if bill_discount > max_discount:
        raise ValidationError(f"Discount exceeds the configured {settings.max_discount_percent}% limit.")
    total = subtotal - total_discount - bill_discount + total_tax
    if total < 0:
        total = Decimal("0")
    amount_received = Decimal(str(amount_received or "0"))
    if payment_method != PharmacySale.PaymentMethod.CREDIT and amount_received < total:
        raise ValidationError("Amount received must cover the net payable for a cash, card or UPI sale.")
    if payment_method == PharmacySale.PaymentMethod.CREDIT and not patient:
        raise ValidationError("Authorised credit needs a registered patient.")
    sale.subtotal = subtotal.quantize(Decimal("0.01"))
    sale.discount = (total_discount + bill_discount).quantize(Decimal("0.01"))
    sale.tax = total_tax
    sale.total = total.quantize(Decimal("0.01"))
    sale.amount_received = amount_received.quantize(Decimal("0.01"))
    sale.payment_method = payment_method
    sale.payment_reference = (payment_reference or "")[:100]
    sale.controlled_authorisation = (controlled_authorisation or "").strip()[:120]
    sale.reference = f"PH-SALE-{sale.pk:06d}"
    sale.save()
    for line in sale.lines.select_related("medicine", "batch"):
        updated = MedicineBatch.objects.filter(
            pk=line.batch_id, status=MedicineBatch.Status.ACTIVE,
            expiry_date__gte=timezone.localdate(), on_hand__gte=F("reserved") + F("damaged") + F("quarantined") + line.quantity,
        ).update(on_hand=F("on_hand") - line.quantity)
        if not updated:
            raise ValidationError(f"{line.medicine_name}: stock changed during checkout. Refresh and retry.")
        StockMovement.objects.create(batch=line.batch, quantity=-line.quantity, kind="sale",
                                     reason=f"Direct sale {sale.reference}", actor=user, sale=sale, request_key=None)
    sale.status = PharmacySale.Status.COMPLETED
    sale.completed_at = timezone.now()
    sale.completed_by = user
    sale.save()
    pharmacy_audit(user, "sale_completed", f"Direct sale {sale.reference} · ₹{sale.total}",
                   entity="PharmacySale", entity_id=sale.pk, after={"total": str(sale.total),
                   "payment_method": payment_method, "items": sale.lines.count(),
                   "controlled_authorisation": sale.controlled_authorisation}, request_key=key)
    if patient:
        event(user, patient, "pharmacy_sale", f"Direct pharmacy sale {sale.reference} · ₹{sale.total}",
              sale_id=sale.pk, total=str(sale.total))
    return sale


@transaction.atomic
def cancel_sale(*, user, sale, reason):
    require_pharmacy(user)
    sale = PharmacySale.objects.select_related("patient").get(pk=sale.pk)
    if sale.status == PharmacySale.Status.CANCELLED:
        return sale
    if sale.status != PharmacySale.Status.COMPLETED:
        raise ValidationError("Only completed sales can be cancelled.")
    if reason.strip() == "":
        raise ValidationError("Record a cancellation reason.")
    returned = PharmacyReturn.objects.filter(sale=sale).exclude(status=PharmacyReturn.Status.REJECTED)
    if returned.exists():
        raise ValidationError("This sale has recorded returns. Review the quarantine and returns register before cancelling.")
    cancelled = PharmacySale.objects.filter(pk=sale.pk, status=PharmacySale.Status.COMPLETED).update(
        status=PharmacySale.Status.CANCELLED)
    if not cancelled:
        raise ValidationError("This sale changed. Refresh and retry.")
    for line in sale.lines.select_related("batch"):
        MedicineBatch.objects.filter(pk=line.batch_id).update(on_hand=F("on_hand") + line.quantity)
        StockMovement.objects.create(batch=line.batch, quantity=line.quantity, kind="reversal",
                                     reason=f"Cancelled sale {sale.reference}: {reason}"[:255], actor=user, sale=sale)
    pharmacy_audit(user, "sale_cancelled", f"Direct sale {sale.reference} cancelled", entity="PharmacySale",
                   entity_id=sale.pk, reason=reason, after={"reason": reason})
    return sale


# ── Procurement ──────────────────────────────────────────────────────────────

@transaction.atomic
def receive_purchase(*, user, supplier, reference, invoice_reference, lines, request_key):
    """Post a goods receipt once; stock increases only after this succeeds."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    existing = PharmacyPurchase.objects.filter(request_key=key).first()
    if existing:
        return existing
    if not lines:
        raise ValidationError("Add at least one purchase line before receiving stock.")
    purchase = PharmacyPurchase.objects.create(
        supplier=supplier, reference=reference.strip()[:120], invoice_reference=invoice_reference.strip()[:100],
        status=PharmacyPurchase.Status.ORDERED, created_by=user, request_key=key,
    )
    total = Decimal("0")
    for spec in lines:
        medicine = Medicine.objects.filter(pk=spec.get("medicine_id"), is_active=True).first()
        if not medicine:
            raise ValidationError("Choose an active medicine on every purchase line.")
        try:
            quantity = int(spec.get("quantity"))
            cost = Decimal(str(spec.get("purchase_cost", "0")))
            price = Decimal(str(spec.get("selling_price", "0")))
        except Exception:
            raise ValidationError("Enter valid quantities and prices on every purchase line.")
        if quantity < 1 or cost < 0 or price < 0:
            raise ValidationError("Purchase quantities must be positive and prices non-negative.")
        expiry = spec.get("expiry_date")
        if expiry and expiry < timezone.localdate():
            raise ValidationError(f"{medicine.name}: cannot receive an expired batch.")
        line = PharmacyPurchaseLine.objects.create(
            purchase=purchase, medicine=medicine, batch_number=(spec.get("batch_number") or "")[:100],
            expiry_date=expiry, quantity_ordered=quantity, purchase_cost=cost,
            selling_price=price, mrp=spec.get("mrp"),
        )
        total += quantity * cost
        if spec.get("batch_number") and expiry:
            receive_stock(user=user, medicine=medicine, batch_number=line.batch_number,
                          expiry_date=expiry, supplier=supplier.name, invoice_reference=invoice_reference,
                          purchase_cost=cost, selling_price=price, quantity_units=quantity,
                          mrp=spec.get("mrp"), request_key=uuid4())
            line.quantity_received = quantity
            line.save(update_fields=["quantity_received"])
    purchase.total = total.quantize(Decimal("0.01"))
    purchase.status = PharmacyPurchase.Status.RECEIVED
    purchase.received_by = user
    purchase.received_at = timezone.now()
    purchase.save()
    PharmacySupplier.objects.filter(pk=supplier.pk).update(outstanding_balance=F("outstanding_balance") + purchase.total)
    pharmacy_audit(user, "purchase_received", f"Goods receipt {purchase.reference} · ₹{purchase.total}",
                   entity="PharmacyPurchase", entity_id=purchase.pk,
                   after={"supplier": supplier.name, "total": str(purchase.total)}, request_key=key)
    return purchase


@transaction.atomic
def record_purchase_order(*, user, supplier, reference, invoice_reference, lines, request_key):
    """Record a supplier order without touching stock or the payables balance."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    existing = PharmacyPurchase.objects.filter(request_key=key).first()
    if existing:
        return existing
    reference = reference.strip()
    if not reference:
        raise ValidationError("Enter a purchase reference.")
    if PharmacyPurchase.objects.filter(reference__iexact=reference).exclude(
            status=PharmacyPurchase.Status.CANCELLED).exists():
        raise ValidationError("A purchase with this reference already exists.")
    if not lines:
        raise ValidationError("Add at least one purchase line before recording the order.")
    purchase = PharmacyPurchase.objects.create(
        supplier=supplier, reference=reference[:120], invoice_reference=invoice_reference.strip()[:100],
        status=PharmacyPurchase.Status.ORDERED, created_by=user, request_key=key,
    )
    total = Decimal("0")
    for spec in lines:
        medicine = Medicine.objects.filter(pk=spec.get("medicine_id"), is_active=True).first()
        if not medicine:
            raise ValidationError("Choose an active medicine on every purchase line.")
        try:
            quantity = int(spec.get("quantity"))
            cost = Decimal(str(spec.get("purchase_cost", "0")))
            price = Decimal(str(spec.get("selling_price", "0")))
        except Exception:
            raise ValidationError("Enter valid quantities and prices on every purchase line.")
        if quantity < 1 or cost < 0 or price < 0:
            raise ValidationError("Purchase quantities must be positive and prices non-negative.")
        expiry = spec.get("expiry_date")
        if expiry and expiry < timezone.localdate():
            raise ValidationError(f"{medicine.name}: an ordered batch cannot carry a past expiry date.")
        PharmacyPurchaseLine.objects.create(
            purchase=purchase, medicine=medicine, batch_number=(spec.get("batch_number") or "")[:100],
            expiry_date=expiry, quantity_ordered=quantity, purchase_cost=cost,
            selling_price=price, mrp=spec.get("mrp"),
        )
        total += quantity * cost
    purchase.total = total.quantize(Decimal("0.01"))
    purchase.save(update_fields=["total"])
    pharmacy_audit(user, "purchase_ordered", f"Purchase order {purchase.reference} · ₹{purchase.total}",
                   entity="PharmacyPurchase", entity_id=purchase.pk,
                   after={"supplier": supplier.name, "total": str(purchase.total),
                          "lines": purchase.lines.count()}, request_key=key)
    return purchase


@transaction.atomic
def receive_ordered_purchase(*, user, purchase):
    """Receive every ordered line that already has batch details; leave the rest open."""
    require_pharmacy(user)
    purchase = PharmacyPurchase.objects.select_related("supplier").get(pk=purchase.pk)
    if purchase.status == PharmacyPurchase.Status.CANCELLED:
        raise ValidationError("A cancelled purchase cannot be received.")
    lines = list(purchase.lines.select_related("medicine"))
    open_lines = [line for line in lines if line.quantity_received < line.quantity_ordered]
    if not open_lines:
        return purchase, 0, []
    ready = [line for line in open_lines if line.batch_number and line.expiry_date]
    missing = [line for line in open_lines if not (line.batch_number and line.expiry_date)]
    if not ready:
        names = ", ".join(line.medicine.name for line in missing[:4])
        raise ValidationError(
            f"Add a batch number and expiry date before receiving: {names}.")
    received_value = Decimal("0")
    for line in ready:
        remaining = line.quantity_ordered - line.quantity_received
        if remaining < 1:
            continue
        if line.expiry_date < timezone.localdate():
            raise ValidationError(f"{line.medicine.name}: the batch on this order has expired. Update the batch details.")
        receive_stock(user=user, medicine=line.medicine, batch_number=line.batch_number,
                      expiry_date=line.expiry_date, supplier=purchase.supplier.name,
                      invoice_reference=purchase.invoice_reference,
                      purchase_cost=line.purchase_cost, selling_price=line.selling_price,
                      quantity_units=remaining, mrp=line.mrp, request_key=uuid4())
        line.quantity_received = line.quantity_ordered
        line.save(update_fields=["quantity_received"])
        received_value += remaining * line.purchase_cost
    received_value = received_value.quantize(Decimal("0.01"))
    still_open = purchase.lines.filter(quantity_received__lt=F("quantity_ordered")).exists()
    purchase.status = PharmacyPurchase.Status.PARTIAL if still_open else PharmacyPurchase.Status.RECEIVED
    purchase.received_by = user
    purchase.received_at = timezone.now()
    purchase.save(update_fields=["status", "received_by", "received_at"])
    if received_value:
        PharmacySupplier.objects.filter(pk=purchase.supplier_id).update(
            outstanding_balance=F("outstanding_balance") + received_value)
    pharmacy_audit(
        user, "purchase_received",
        f"Goods received for {purchase.reference} · ₹{received_value}",
        entity="PharmacyPurchase", entity_id=purchase.pk,
        after={"value": str(received_value), "status": purchase.status,
               "pending_lines": [line.medicine.name for line in missing]},
    )
    return purchase, received_value, missing


@transaction.atomic
def set_purchase_line_details(*, user, line, batch_number, expiry_date):
    """Capture batch details on an ordered line before it is received."""
    require_pharmacy(user)
    line = PharmacyPurchaseLine.objects.select_related("purchase", "medicine").get(pk=line.pk)
    if line.purchase.status == PharmacyPurchase.Status.CANCELLED:
        raise ValidationError("A cancelled purchase cannot be edited.")
    if line.quantity_received >= line.quantity_ordered:
        raise ValidationError("This line is already received; its batch details are locked.")
    if not batch_number.strip():
        raise ValidationError("Enter the batch number supplied on the invoice.")
    if expiry_date < timezone.localdate():
        raise ValidationError("The batch expiry cannot be in the past.")
    line.batch_number = batch_number.strip()[:100]
    line.expiry_date = expiry_date
    line.save(update_fields=["batch_number", "expiry_date"])
    pharmacy_audit(user, "purchase_line_updated",
                   f"Batch details set for {line.medicine.name} on {line.purchase.reference}",
                   entity="PharmacyPurchase", entity_id=line.purchase_id,
                   after={"batch": line.batch_number, "expiry": str(line.expiry_date)})
    return line


# ── Returns, holds and transfers ─────────────────────────────────────────────

@transaction.atomic
def create_patient_return(*, user, line, quantity, reason, request_key):
    """Take returned medicine into quarantine; never straight back to sellable stock."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    existing = PharmacyReturn.objects.filter(request_key=key).first()
    if existing:
        return existing
    quantity = int(quantity)
    # A rejected return never re-entered stock, so it does not consume the sold quantity.
    already = PharmacyReturn.objects.filter(
        sale_line=line, status__in=("pending", "quarantined", "completed"),
    ).aggregate(n=Sum("quantity"))["n"] or 0
    if quantity < 1 or quantity + already > line.quantity:
        raise ValidationError("Return quantity exceeds the quantity sold on this line.")
    refund = line.unit_price * quantity
    if reason.strip() == "":
        raise ValidationError("Record a return reason.")
    ret = PharmacyReturn.objects.create(
        kind=PharmacyReturn.Kind.PATIENT, sale=line.sale, sale_line=line, batch=line.batch,
        quantity=quantity, reason=reason[:255], refund=refund, status=PharmacyReturn.Status.PENDING,
        created_by=user, request_key=key,
    )
    updated = MedicineBatch.objects.filter(pk=line.batch_id).update(
        on_hand=F("on_hand") + quantity, quarantined=F("quarantined") + quantity)
    if not updated:
        raise ValidationError("The batch inventory changed. Refresh and retry the return.")
    ret.status = PharmacyReturn.Status.QUARANTINED
    ret.reviewed_by = user
    ret.reviewed_at = timezone.now()
    ret.save()
    StockMovement.objects.create(batch=line.batch, quantity=quantity, kind="sale_return",
                                 reason=f"Patient return (quarantined) · {ret.reason}"[:255], actor=user, sale=line.sale)
    pharmacy_audit(user, "patient_return", f"Patient return of {quantity} {line.medicine_name} quarantined",
                   entity="PharmacyReturn", entity_id=ret.pk, after={"refund": str(refund)}, reason=reason, request_key=key)
    return ret


@transaction.atomic
def return_to_supplier(*, user, batch, quantity, reason, request_key):
    """Send unsellable or excess stock back to the supplier and reduce physical stock."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    existing = PharmacyReturn.objects.filter(request_key=key).first()
    if existing:
        return existing
    quantity = int(quantity)
    batch = MedicineBatch.objects.get(pk=batch.pk)
    if quantity < 1:
        raise ValidationError("Supplier return quantity must be positive.")
    if reason.strip() == "":
        raise ValidationError("Record why the stock is going back to the supplier.")
    # Only physically free stock can leave; reserved units belong to a prepared issue.
    free = batch.on_hand - batch.reserved
    if quantity > free:
        raise ValidationError(f"Only {max(free, 0)} unreserved units can be returned to the supplier.")
    # Take held units first so the counters keep matching the physical count.
    held = min(quantity, batch.quarantined)
    damaged = min(quantity - held, batch.damaged)
    updated = MedicineBatch.objects.filter(
        pk=batch.pk, on_hand__gte=F("reserved") + quantity,
        quarantined__gte=held, damaged__gte=damaged,
    ).update(on_hand=F("on_hand") - quantity,
             quarantined=F("quarantined") - held, damaged=F("damaged") - damaged)
    if not updated:
        raise ValidationError("Stock changed while preparing the supplier return. Refresh and retry.")
    credit = batch.purchase_cost * quantity
    ret = PharmacyReturn.objects.create(
        kind=PharmacyReturn.Kind.SUPPLIER, batch=batch, quantity=quantity, reason=reason[:255],
        refund=credit.quantize(Decimal("0.01")), status=PharmacyReturn.Status.COMPLETED,
        created_by=user, reviewed_by=user, reviewed_at=timezone.now(), request_key=key,
    )
    StockMovement.objects.create(batch=batch, quantity=-quantity, kind="purchase_return",
                                 reason=f"Supplier return · {reason}"[:255], actor=user)
    pharmacy_audit(user, "supplier_return", f"{quantity} units of {batch} returned to the supplier",
                   entity="PharmacyReturn", entity_id=ret.pk, after={"credit": str(ret.refund)},
                   reason=reason, request_key=key)
    return ret


@transaction.atomic
def settle_patient_return(*, user, ret, request_key):
    """Mark the cash refund as handed back, so the register shows it is closed."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    if PharmacyAudit.objects.filter(request_key=key).exists():
        return ret
    ret = PharmacyReturn.objects.get(pk=ret.pk)
    if ret.kind != PharmacyReturn.Kind.PATIENT:
        raise ValidationError("Only patient returns carry a refund.")
    if ret.status == PharmacyReturn.Status.COMPLETED:
        return ret
    if ret.status != PharmacyReturn.Status.QUARANTINED:
        raise ValidationError("Only a quarantined return can have its refund settled.")
    settled = PharmacyReturn.objects.filter(
        pk=ret.pk, status=PharmacyReturn.Status.QUARANTINED,
    ).update(status=PharmacyReturn.Status.COMPLETED, reviewed_by=user, reviewed_at=timezone.now())
    if not settled:
        raise ValidationError("This return changed. Refresh the register.")
    pharmacy_audit(user, "patient_refund", f"Refund of ₹{ret.refund} settled for return #{ret.pk}",
                   entity="PharmacyReturn", entity_id=ret.pk, after={"refund": str(ret.refund)},
                   request_key=key)
    return ret


@transaction.atomic
def release_quarantine(*, user, batch, quantity, reason, request_key, target="active"):
    """Authorised assessment: release quarantine back to sale, or move to damaged/write-off."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    if StockMovement.objects.filter(request_key=key).exists():
        return
    quantity = int(quantity)
    batch = MedicineBatch.objects.get(pk=batch.pk)
    if quantity < 1 or quantity > batch.quarantined:
        raise ValidationError("Only quarantined stock can be released, up to its held quantity.")
    if reason.strip() == "":
        raise ValidationError("Record the assessment reason.")
    if target == "damaged":
        updated = MedicineBatch.objects.filter(pk=batch.pk, quarantined__gte=quantity).update(quarantined=F("quarantined") - quantity, damaged=F("damaged") + quantity)
    elif target == "active":
        updated = MedicineBatch.objects.filter(pk=batch.pk, quarantined__gte=quantity).update(quarantined=F("quarantined") - quantity)
    elif target == "writeoff":
        updated = MedicineBatch.objects.filter(pk=batch.pk, quarantined__gte=quantity, on_hand__gte=quantity).update(quarantined=F("quarantined") - quantity, on_hand=F("on_hand") - quantity)
    else:
        raise ValidationError("Choose a valid quarantine outcome.")
    if not updated:
        raise ValidationError("The quarantine hold changed. Refresh and retry.")
    StockMovement.objects.create(batch=batch, quantity=-quantity if target == "writeoff" else 0,
                                 kind="quarantine" if target != "writeoff" else "expired",
                                 reason=f"Quarantine {target}: {reason}"[:255], actor=user, request_key=key)
    pharmacy_audit(user, "quarantine_released", f"{quantity} units of {batch} -> {target}",
                   entity="MedicineBatch", entity_id=batch.pk, reason=reason, request_key=key)


@transaction.atomic
def hold_quarantine(*, user, batch, quantity, reason, request_key):
    """Hold usable stock for review without moving it out of the physical count."""
    require_pharmacy(user)
    key = request_uuid(request_key)
    if StockMovement.objects.filter(request_key=key).exists():
        return
    quantity = int(quantity)
    batch = MedicineBatch.objects.get(pk=batch.pk)
    if quantity < 1 or quantity > batch_usable_quantity(batch):
        raise ValidationError("Quarantine quantity must be positive and within unreserved stock.")
    if reason.strip() == "":
        raise ValidationError("Record why this stock is being quarantined.")
    updated = MedicineBatch.objects.filter(
        pk=batch.pk, on_hand__gte=F("reserved") + F("damaged") + F("quarantined") + quantity,
    ).update(quarantined=F("quarantined") + quantity)
    if not updated:
        raise ValidationError("Stock changed. Refresh and retry.")
    StockMovement.objects.create(batch=batch, quantity=0, kind="quarantine",
                                 reason=f"{quantity} quarantined: {reason}"[:255], actor=user, request_key=key)
    pharmacy_audit(user, "stock_quarantined", f"{quantity} units of {batch} quarantined",
                   entity="MedicineBatch", entity_id=batch.pk, reason=reason, request_key=key)


@transaction.atomic
def record_damage(*, user, batch, quantity, reason, request_key):
    require_pharmacy(user)
    key = request_uuid(request_key)
    if StockMovement.objects.filter(request_key=key).exists():
        return
    quantity = int(quantity)
    batch = MedicineBatch.objects.get(pk=batch.pk)
    if quantity < 1 or quantity > batch_usable_quantity(batch):
        raise ValidationError("Damaged quantity must be positive and within unreserved stock.")
    updated = MedicineBatch.objects.filter(pk=batch.pk, on_hand__gte=F("reserved") + F("damaged") + F("quarantined") + quantity).update(damaged=F("damaged") + quantity)
    if not updated:
        raise ValidationError("Stock changed. Refresh and retry.")
    StockMovement.objects.create(batch=batch, quantity=0, kind="damaged",
                                 reason=f"{quantity} damaged: {reason}"[:255], actor=user, request_key=key)
    pharmacy_audit(user, "damage_recorded", f"{quantity} units of {batch} marked damaged",
                   entity="MedicineBatch", entity_id=batch.pk, reason=reason, request_key=key)


@transaction.atomic
def transfer_stock(*, user, batch, quantity, to_location, reason, request_key):
    require_pharmacy(user)
    key = request_uuid(request_key)
    existing = StockTransfer.objects.filter(request_key=key).first()
    if existing:
        return existing
    quantity = int(quantity)
    batch = MedicineBatch.objects.get(pk=batch.pk)
    if quantity < 1 or quantity > batch_usable_quantity(batch):
        raise ValidationError("Transfer quantity must be positive and within unreserved stock.")
    if not to_location.strip():
        raise ValidationError("Enter the destination location.")
    StockTransfer.objects.create(batch=batch, from_location=batch.storage_location or "Main pharmacy",
                                 to_location=to_location[:100], quantity=quantity, requested_by=user,
                                 completed_by=user, status="completed", request_key=key,
                                 completed_at=timezone.now())
    MedicineBatch.objects.filter(pk=batch.pk).update(storage_location=to_location[:100])
    StockMovement.objects.create(batch=batch, quantity=0, kind="transfer",
                                 reason=f"Transfer to {to_location}: {reason}"[:255], actor=user, request_key=key)
    pharmacy_audit(user, "stock_transfer", f"{quantity} units of {batch} -> {to_location}",
                   entity="MedicineBatch", entity_id=batch.pk, reason=reason, request_key=key)
