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
)


def designation(user):
    return getattr(getattr(user, "profile", None), "designation", "")


def is_admin(user):
    return user.is_superuser or designation(user) == "admin"


def require_pharmacy(user):
    if not is_admin(user) and designation(user) != "pharmacy":
        raise PermissionDenied("Pharmacy access required.")


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
    if not is_admin(user) and designation(user) != "registration_desk":
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
def receive_stock(*, user, medicine, batch_number, expiry_date, supplier, invoice_reference, purchase_cost, selling_price, request_key, packs=None, quantity_units=None):
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
        "purchase_cost": purchase_cost, "selling_price": selling_price,
    })
    if not created and (batch.expiry_date != expiry_date or batch.purchase_cost != purchase_cost or batch.selling_price != selling_price):
        raise ValidationError("This batch already exists with different expiry or prices. Check the receipt.")
    MedicineBatch.objects.filter(pk=batch.pk).update(on_hand=F("on_hand") + quantity)
    StockMovement.objects.create(batch=batch, quantity=quantity, kind="receipt", reason=f"Receipt {invoice_reference} · {supplier}"[:255], actor=user, request_key=key)
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
        for batch in MedicineBatch.objects.filter(medicine=medicine, expiry_date__gte=timezone.localdate(), on_hand__gt=F("reserved")).order_by("expiry_date", "id"):
            take = min(remaining, batch.on_hand - batch.reserved)
            if not MedicineBatch.objects.filter(pk=batch.pk, on_hand__gte=F("reserved") + take).update(reserved=F("reserved") + take):
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
        if allocation.batch.expiry_date < timezone.localdate():
            raise ValidationError("A reserved batch has expired. Receive and reallocate a valid batch before handover.")
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
        for batch in MedicineBatch.objects.filter(medicine_id=line.medicine_id, expiry_date__gte=timezone.localdate(), on_hand__gt=F("reserved")).order_by("expiry_date", "id"):
            take = min(remaining, batch.on_hand - batch.reserved)
            if not MedicineBatch.objects.filter(pk=batch.pk, on_hand__gte=F("reserved") + take).update(reserved=F("reserved") + take):
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
    if not is_admin(user) and designation(user) != "registration_desk":
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
