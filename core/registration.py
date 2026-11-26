"""Shared OPD registration helpers used by the Registration Desk counters.

Keeping the visit/bill creation in one place means a brand-new registration and
an OPD revisit produce exactly the same appointment + registration bill shape.
"""

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from appointments.models import Appointment
from patients.models import Patient, PatientBill


def _dec(value):
    """Convert a form/post value to Decimal, treating blanks and junk as zero."""
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def create_opd_visit_for_patient(
    patient,
    *,
    user,
    reg_fee=None,
    con_fee=None,
    discount=None,
    reason="New patient registration - OPD",
):
    """Create today's OPD appointment for a patient and its registration bill.

    No doctor is chosen here; the OPD assistant assigns one from the central
    pool. A registration/consultation bill is raised only when the net amount
    after discount is positive. Returns ``(appointment, bill)`` where ``bill``
    is ``None`` when nothing is collectable.
    """
    reg = _dec(reg_fee)
    con = _dec(con_fee)
    gross = reg + con
    disc = max(_dec(discount), Decimal("0"))
    # A discount can never make the bill negative; clamp it to the gross amount.
    disc = min(disc, gross)

    with transaction.atomic():
        appointment = Appointment.objects.create(
            patient=patient,
            doctor=None,
            date=timezone.localdate(),
            start_time=timezone.localtime().time().replace(second=0, microsecond=0),
            duration_minutes=30,
            status=Appointment.Status.SCHEDULED,
            reason=reason,
        )

        bill = None
        if gross - disc > 0:
            description = (
                "Patient registration & consultation fee"
                if "New" in reason
                else "OPD revisit registration & consultation fee"
            )
            bill = PatientBill.objects.create(
                patient=patient,
                source=PatientBill.Source.REGISTRATION,
                appointment=appointment,
                description=description,
                amount=gross,
                discount=disc,
                created_by=user,
            )

    return appointment, bill


def find_potential_duplicates(phone="", aadhar_no=""):
    """Look for existing patients that may already hold this identity.

    A phone is only worth searching with at least 6 characters and an Aadhar
    number with at least 4, so the desk does not get flooded with weak matches.
    """
    phone = (phone or "").strip()
    aadhar_no = (aadhar_no or "").strip()

    query = Q()
    if len(phone) >= 6:
        query |= Q(phone__icontains=phone)
    if len(aadhar_no) >= 4:
        query |= Q(aadhar_no__icontains=aadhar_no)
    if not query:
        return []

    matches = []
    for patient in Patient.objects.filter(query).order_by("-created_at")[:5]:
        matches.append({
            "pk": patient.pk,
            "uhid": patient.uhid,
            "full_name": patient.full_name,
            "phone": patient.phone,
            "aadhar_no": patient.aadhar_no,
        })
    return matches
