from datetime import date, timedelta
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie


from .models import UserProfile
from .operations import (
    appointment_pre_visit_bills,
    appointment_pre_visit_pending,
    consultation_pending_bills,
    guarded_consultation_save,
    pay_bills,
    prescription_payload,
    sync_prescriptions,
    sync_service_bills,
)
from .operations_views import accessible_reports
from .backup import BackupArchiveError, build_patient_backup, import_patient_backup


def home(request):
    """Redirect to login or appropriate dashboard based on user designation."""
    if request.user.is_authenticated:
        user_desig = normalize_designation(
            getattr(getattr(request.user, "profile", None), "designation", None)
            or request.session.get("designation")
        )
        if user_desig == "registration_desk":
            return redirect("reception_dashboard")
        if user_desig == UserProfile.Designation.ADMIN:
            return redirect("admin_dashboard")
        if user_desig == UserProfile.Designation.SERVICE_STAFF:
            return redirect("service_staff_dashboard")
        if user_desig == UserProfile.Designation.PHARMACY:
            return redirect("pharmacy_dashboard")
        return redirect("dashboard")
    return redirect("login")

from appointments.models import Appointment, Doctor
from patients.models import Patient, OPDAuditEvent, PatientBill


# ── Login / logout ────────────────────────────────────────────────────────────

DESIGNATION_CHOICES = [
    ("registration_desk", "Registration Desk"),
    ("opd_assistant", "OPD Assistant"),
    ("doctor", "Doctor"),
    ("service_staff", "Service Staff"),
    ("pharmacy", "Pharmacy"),
    ("admin", "Admin"),
]


def normalize_designation(designation):
    """Map legacy stored designations to their merged replacement.

    The standalone Nurse designation was merged into OPD Assistant. Accounts
    whose profile still stores ``nurse`` keep working — they are treated as
    OPD assistants — until an administrator edits the profile manually.
    """
    if designation == "nurse":
        return UserProfile.Designation.OPD_ASSISTANT
    return designation

SERVICE_STAFF_QUEUES = [
    ("laboratory", "Laboratory"),
    ("radiology", "Radiology · X-Ray & USG"),
    ("cardiology", "Cardiology · ECG"),
    ("physiotherapy", "Physiotherapy"),
    ("counselling", "Counselling"),
    ("procedure-room", "Procedure Room"),
    ("procedures", "Procedures"),
]


@never_cache
@ensure_csrf_cookie
def login_view(request):
    """Custom login page with designation selector and server-side role validation."""
    if request.user.is_authenticated:
        return redirect("home")

    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        designation = request.POST.get("designation", "")

        user = authenticate(request, username=username, password=password)
        if user is not None and user.is_active:
            # Ensure a UserProfile exists for this user
            profile, _ = UserProfile.objects.get_or_create(
                user=user,
                defaults={
                    "designation": (
                        UserProfile.Designation.ADMIN
                        if user.is_superuser
                        else UserProfile.Designation.REGISTRATION_DESK
                    )
                },
            )

            # The selected role must match the role assigned to the account.
            # Superuser status alone must not let an account switch into Admin.
            # Legacy profiles storing 'nurse' are treated as OPD Assistant.
            effective_designation = normalize_designation(profile.designation)
            if effective_designation != designation:
                allowed_label = dict(UserProfile.Designation.choices).get(
                    effective_designation, effective_designation
                )
                chosen_label = dict(UserProfile.Designation.choices).get(designation, designation)
                error = (
                    f"Access denied: User '{user.username}' is allocated to '{allowed_label}' only. "
                    f"You cannot log in as '{chosen_label}'."
                )
                return render(request, "core/login.html", {
                    "error": error,
                    "designation_choices": DESIGNATION_CHOICES,
                })

            login(request, user)
            request.session["designation"] = effective_designation

            if effective_designation == "registration_desk":
                return redirect("reception_dashboard")
            if effective_designation == "opd_assistant":
                return redirect("opd_assistant_dashboard")
            if effective_designation == UserProfile.Designation.ADMIN:
                return redirect("admin_dashboard")
            if effective_designation == UserProfile.Designation.SERVICE_STAFF:
                return redirect("service_staff_dashboard")
            if effective_designation == UserProfile.Designation.PHARMACY:
                return redirect("pharmacy_dashboard")
            else:
                return redirect("dashboard")
        else:
            error = "Invalid username or password."

    return render(request, "core/login.html", {
        "error": error,
        "designation_choices": DESIGNATION_CHOICES,
    })


def logout_view(request):
    """Log out the current user and redirect to login."""
    logout(request)
    return redirect("login")


def csrf_failure(request, reason=""):
    """Show a recoverable message instead of Django's technical CSRF page."""
    if request.path == "/login/":
        return render(request, "core/login.html", {
            "error": "Your sign-in form expired. Please try again.",
            "designation_choices": DESIGNATION_CHOICES,
        }, status=400)
    return HttpResponseForbidden("Your form security check expired. Refresh the page and try again.")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@login_required(login_url="login")
def dashboard(request):
    """Doctor / Clinic overview dashboard."""
    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig == UserProfile.Designation.ADMIN:
        return redirect("admin_dashboard")
    if user_desig == "registration_desk" and not request.user.is_superuser:
        from django.contrib import messages
        messages.warning(request, "Access restricted: Registration Desk users only have access to the Registration Desk portal.")
        return redirect("reception_dashboard")
    if user_desig == "opd_assistant" and not request.user.is_superuser:
        from django.contrib import messages
        messages.warning(request, "Access restricted: OPD Assistants use the OPD Desk portal.")
        return redirect("opd_assistant_dashboard")
    if user_desig == UserProfile.Designation.SERVICE_STAFF and not request.user.is_superuser:
        return redirect("service_staff_dashboard")
    if user_desig == UserProfile.Designation.PHARMACY and not request.user.is_superuser:
        return redirect("pharmacy_dashboard")

    today = date.today()
    from patients.models import (
        ClinicalOrder, ConsultationSession, PrescriptionItem,
    )
    from django.db.models import Q

    # Scope data to the signed-in doctor's own workload when applicable.
    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
    is_doctor = (user_desig == UserProfile.Designation.DOCTOR) and not request.user.is_superuser
    doctor_scope = Q(assigned_doctor=request.user) if is_doctor else Q()
    consult_scope = Q(consultation__doctor=request.user) if is_doctor else Q()
    session_scope = Q(doctor=request.user) if is_doctor else Q()

    q = request.GET.get("q", "").strip()
    queue_qs = Patient.objects.all().select_related("assigned_doctor", "assigned_doctor__profile").prefetch_related("bills")
    if is_doctor:
        queue_qs = queue_qs.filter(assigned_doctor=request.user)
    if q:
        queue_qs = queue_qs.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(uhid__icontains=q) |
            Q(phone__icontains=q)
        )
    queue_qs = queue_qs.order_by("-created_at")[:50]

    # Per-department patient previews (distinct patients, newest first).
    def _distinct_patients_from_qs(qs, attr_patient="patient", limit=4):
        seen = set()
        out = []
        for obj in qs:
            p = getattr(obj, attr_patient, None)
            if p is None and hasattr(obj, "consultation"):
                p = obj.consultation.patient
            if p is not None and p.pk not in seen:
                seen.add(p.pk)
                out.append(p)
                if len(out) >= limit:
                    break
        return out

    if is_doctor:
        rx_base = PrescriptionItem.objects.filter(consultation__doctor=request.user, dispense_status__in=["ordered", "partial"])
    else:
        rx_base = PrescriptionItem.objects.filter(dispense_status__in=["ordered", "partial"])
    # Build pharmacy patient preview list
    pharmacy_patients = []
    _seen = set()
    for item in rx_base.select_related("consultation__patient").order_by("-created_at"):
        p = item.consultation.patient
        if p.pk not in _seen:
            _seen.add(p.pk)
            pharmacy_patients.append(p)
            if len(pharmacy_patients) >= 4:
                break

    lab_qs = ClinicalOrder.objects.filter(order_type="laboratory", status__in=["Ordered", "In Progress", "Review Required"])
    if is_doctor:
        lab_qs = lab_qs.filter(consultation__doctor=request.user)
    lab_patients = []
    _seen = set()
    for o in lab_qs.select_related("consultation__patient").order_by("-created_at"):
        p = o.consultation.patient
        if p.pk not in _seen:
            _seen.add(p.pk)
            lab_patients.append(p)
            if len(lab_patients) >= 4:
                break

    img_qs = ClinicalOrder.objects.filter(order_type="radiology", status__in=["Ordered", "In Progress", "Review Required"])
    if is_doctor:
        img_qs = img_qs.filter(consultation__doctor=request.user)
    imaging_patients = []
    _seen = set()
    for o in img_qs.select_related("consultation__patient").order_by("-created_at"):
        p = o.consultation.patient
        if p.pk not in _seen:
            _seen.add(p.pk)
            imaging_patients.append(p)
            if len(imaging_patients) >= 4:
                break

    # USG-specific: radiology orders whose name contains usg
    usg_qs = img_qs.filter(name__icontains="usg")
    usg_patients = []
    _seen = set()
    for o in usg_qs.select_related("consultation__patient").order_by("-created_at"):
        p = o.consultation.patient
        if p.pk not in _seen:
            _seen.add(p.pk)
            usg_patients.append(p)
            if len(usg_patients) >= 4:
                break
    if not usg_patients:
        usg_patients = imaging_patients[:4]

    physio_qs = ClinicalOrder.objects.filter(order_type="other", status__in=["Ordered", "In Progress"])
    if is_doctor:
        physio_qs = physio_qs.filter(consultation__doctor=request.user)
    physio_qs = physio_qs.filter(Q(name__icontains="physio") | Q(name__icontains="counsel") | Q(name__icontains="procedure"))
    physio_patients = []
    _seen = set()
    for o in physio_qs.select_related("consultation__patient").order_by("-created_at"):
        p = o.consultation.patient
        if p.pk not in _seen:
            _seen.add(p.pk)
            physio_patients.append(p)
            if len(physio_patients) >= 4:
                break

    from patients.models import OPDReferral
    ref_qs = OPDReferral.objects.filter(status__in=["draft", "sent"])
    if is_doctor:
        ref_qs = ref_qs.filter(Q(created_by=request.user) | Q(doctor=request.user))
    cross_patients = []
    _seen = set()
    for r in ref_qs.select_related("patient").order_by("-created_at"):
        p = r.patient
        if p.pk not in _seen:
            _seen.add(p.pk)
            cross_patients.append(p)
            if len(cross_patients) >= 2:
                break

    context = {
        "today": today,
        "q": q,
        "total_patients": Patient.objects.count() if not is_doctor else Patient.objects.filter(assigned_doctor=request.user).count(),
        "total_doctors": Doctor.objects.count(),
        "todays_appointments": (
            Appointment.objects.filter(date=today)
            .select_related("patient", "doctor")
            .order_by("start_time")
        ),
        "upcoming_count": Appointment.objects.filter(
            date__gte=today, status=Appointment.Status.SCHEDULED
        ).count(),
        "recent_patients": Patient.objects.order_by("-created_at")[:5],
        "queue_patients": queue_qs,
        "pharmacy_patients": pharmacy_patients,
        "lab_patients": lab_patients,
        "imaging_patients": imaging_patients,
        "usg_patients": usg_patients,
        "physio_patients": physio_patients,
        "cross_patients": cross_patients,
        # KPI strip — real data.
        "kpi_total_today": Patient.objects.filter(created_at__date=today).count() if not is_doctor else Patient.objects.filter(created_at__date=today, assigned_doctor=request.user).count(),
        "kpi_in_pool": Patient.objects.filter(created_at__date=today, is_emergency=False, **({"assigned_doctor": request.user} if is_doctor else {})).count(),
        "kpi_with_doctors": Patient.objects.filter(assigned_doctor__isnull=False, **({"assigned_doctor": request.user} if is_doctor else {})).count(),
        "kpi_investigations": ClinicalOrder.objects.filter(
            consult_scope, order_type__in=["laboratory", "radiology"],
            status__in=["Ordered", "In Progress", "Review Required"],
        ).count(),
        "kpi_procedures": ClinicalOrder.objects.filter(
            consult_scope, order_type="other",
            status__in=["Ordered", "In Progress"],
            name__icontains="physio",
        ).count() + ClinicalOrder.objects.filter(
            consult_scope, order_type="other",
            status__in=["Ordered", "In Progress"],
            name__icontains="procedure",
        ).count(),
        "kpi_pharmacy": (
            PrescriptionItem.objects.filter(consultation__doctor=request.user, dispense_status__in=["ordered", "partial"]).count()
            if is_doctor
            else PrescriptionItem.objects.filter(dispense_status__in=["ordered", "partial"]).count()
        ),
        "kpi_review_required": ConsultationSession.objects.filter(
            session_scope, status__in=["draft", "in_progress"],
        ).count(),
        "kpi_completed": (
            Appointment.objects.filter(date=today, status=Appointment.Status.COMPLETED).count()
            if not is_doctor
            else ConsultationSession.objects.filter(doctor=request.user, status="completed", updated_at__date=today).count()
        ),
        "doctor_accounts": (
            get_user_model()
            .objects.filter(profile__designation=UserProfile.Designation.DOCTOR)
            .order_by("first_name", "username")
        ),
    }
    return render(request, "core/dashboard.html", context)


@login_required(login_url="login")
def assign_doctor(request, patient_id):
    """Assign (or clear) the doctor account looking after a patient."""
    patient = get_object_or_404(Patient, pk=patient_id)

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if designation not in (
        UserProfile.Designation.DOCTOR,
        UserProfile.Designation.ADMIN,
        UserProfile.Designation.OPD_ASSISTANT,
    ) and not request.user.is_superuser:
        messages.warning(
            request,
            "Access restricted: only OPD assistants, doctors, or administrators can assign a patient.",
        )
        return redirect("dashboard")

    if request.method != "POST":
        return redirect("dashboard")

    # Send the user back to the page the assignment was made from, when safe.
    next_url = request.POST.get("next", "")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = ""
    fallback = (
        "opd_assistant_dashboard"
        if designation == UserProfile.Designation.OPD_ASSISTANT
        else "dashboard"
    )

    def _finish():
        if next_url:
            return redirect(next_url)
        return redirect(fallback)

    active_appointment = (
        Appointment.objects.filter(
            patient=patient,
            date__gte=timezone.localdate(),
            status=Appointment.Status.SCHEDULED,
        )
        .order_by("date", "start_time")
        .first()
    )
    if (
        active_appointment is not None
        and appointment_pre_visit_pending(active_appointment)
        and not patient.is_emergency
        and not _is_hospital_admin(request)
    ):
        messages.error(
            request,
            f"{patient.full_name} still has a pending registration/appointment bill. "
            "Collect payment before allocating a doctor.",
        )
        return _finish()

    doctor_id = request.POST.get("doctor_id", "").strip()
    from patients.models import InitialAssessment, NurseQueueTicket
    assessment = getattr(patient, "initial_assessment", None)
    current_ticket = (
        patient.nurse_tickets.filter(
            appointment=active_appointment,
            status=NurseQueueTicket.Status.COMPLETED,
        ).exists()
        if active_appointment is not None
        else False
    )
    assessment_today = bool(
        assessment is not None
        and assessment.status == InitialAssessment.Status.COMPLETED
        and assessment.completed_at is not None
        and timezone.localtime(assessment.completed_at).date() == timezone.localdate()
    )
    if (
        doctor_id
        and not patient.is_emergency
        and not _is_hospital_admin(request)
        and not (current_ticket or assessment_today)
    ):
        messages.error(
            request,
            f"{patient.full_name} needs a completed initial assessment for this visit before doctor allocation.",
        )
        return _finish()
    if not doctor_id:
        previous_doctor = patient.assigned_doctor
        patient.assigned_doctor = None
        patient.save(update_fields=["assigned_doctor"])
        if previous_doctor:
            OPDAuditEvent.objects.create(
                patient=patient,
                actor=request.user,
                action="doctor_unassigned",
                summary=(
                    "Doctor assignment cleared: "
                    f"{previous_doctor.get_full_name() or previous_doctor.username}"
                ),
                details={"doctor_id": previous_doctor.pk},
            )
        messages.success(request, f"{patient.full_name} is now unassigned.")
        return _finish()

    doctor = get_object_or_404(
        get_user_model(),
        pk=doctor_id,
        profile__designation=UserProfile.Designation.DOCTOR,
    )
    patient.assigned_doctor = doctor
    patient.save(update_fields=["assigned_doctor"])
    specialty = doctor.profile.get_specialty_display() if doctor.profile.specialty else "General Medicine"
    OPDAuditEvent.objects.create(
        patient=patient,
        actor=request.user,
        action="doctor_assigned",
        summary=(
            f"Assigned to Dr. {doctor.get_full_name() or doctor.username} ({specialty})"
        ),
        details={"doctor_id": doctor.pk},
    )
    messages.success(
        request,
        f"{patient.full_name} assigned to {doctor.get_full_name() or doctor.username} — {specialty}.",
    )
    return _finish()


@login_required(login_url="login")
def admin_dashboard(request):
    """Hospital-wide patient load dashboard for administrators."""
    profile = getattr(request.user, "profile", None)
    user_desig = getattr(profile, "designation", None)
    if user_desig != UserProfile.Designation.ADMIN:
        from django.contrib import messages
        messages.warning(request, "Access restricted: Hospital administrators only.")
        if user_desig == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        return redirect("dashboard")

    today = date.today()
    week_start = today - timedelta(days=6)
    daily_totals = {
        row["registration_day"]: row["total"]
        for row in Patient.objects.filter(
            created_at__date__gte=week_start,
            created_at__date__lte=today,
        ).annotate(registration_day=TruncDate("created_at"))
        .values("registration_day")
        .annotate(total=Count("id"))
    }
    daily_load = [
        {
            "label": (week_start + timedelta(days=offset)).strftime("%a"),
            "date": week_start + timedelta(days=offset),
            "total": daily_totals.get(week_start + timedelta(days=offset), 0),
        }
        for offset in range(7)
    ]
    max_daily_load = max((item["total"] for item in daily_load), default=0)
    for item in daily_load:
        item["height"] = max(5, round(item["total"] * 100 / max_daily_load)) if max_daily_load else 5

    specialty_names = dict(Doctor.SPECIALTIES)
    department_load = [
        {
            "name": specialty_names.get(row["doctor__specialty"], "Other / Unspecified"),
            "total": row["patient_total"],
        }
        for row in Appointment.objects.filter(date=today)
        .exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW])
        .values("doctor__specialty")
        .annotate(patient_total=Count("patient", distinct=True))
        .order_by("-patient_total", "doctor__specialty")
    ]
    max_department_load = max((item["total"] for item in department_load), default=0)
    for item in department_load:
        item["width"] = round(item["total"] * 100 / max_department_load) if max_department_load else 0

    from patients.models import NurseTask
    open_tasks_qs = NurseTask.objects.filter(status__in=["pending", "in_progress"])
    return render(request, "core/admin_dashboard.html", {
        "today": today,
        "total_patients": Patient.objects.count(),
        "today_count": Patient.objects.filter(created_at__date=today).count(),
        "today_appointments": Appointment.objects.filter(date=today).exclude(
            status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW]
        ).count(),
        "department_count": len(department_load),
        "daily_load": daily_load,
        "department_load": department_load,
        "recent_patients": Patient.objects.order_by("-created_at")[:6],
        "nursing_open": open_tasks_qs.count(),
        "nursing_unassigned": open_tasks_qs.filter(assigned_to__isnull=True).count(),
        "nursing_overdue": sum(1 for t in open_tasks_qs if t.is_overdue),
        "user": request.user,
    })


def _require_admin(request):
    if getattr(request.user, "is_superuser", False):
        return None
    profile = getattr(request.user, "profile", None)
    user_desig = getattr(profile, "designation", None)
    if user_desig == UserProfile.Designation.ADMIN:
        return None
    messages.warning(request, "Access restricted: Hospital administrators only.")
    if user_desig == UserProfile.Designation.REGISTRATION_DESK:
        return redirect("reception_dashboard")
    return redirect("dashboard")


def _require_registration_desk(request):
    if getattr(request.user, "is_superuser", False):
        return None
    designation = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if designation in (UserProfile.Designation.REGISTRATION_DESK, UserProfile.Designation.ADMIN):
        return None
    messages.warning(request, "This console is available to Registration Desk staff only.")
    return redirect("dashboard")


@login_required(login_url="login")
def bill_command_center(request):
    """Find a patient and show the billing amounts currently stored on their record."""
    denied = _require_registration_desk(request)
    if denied:
        return denied

    from django.db.models import Exists, OuterRef, Q

    query = request.GET.get("q", "").strip()
    pending_bills = PatientBill.objects.filter(
        status=PatientBill.Status.PENDING,
    ).select_related("patient", "created_by")
    if query:
        pending_bills = pending_bills.filter(
            Q(patient__first_name__icontains=query)
            | Q(patient__second_name__icontains=query)
            | Q(patient__last_name__icontains=query)
            | Q(patient__uhid__icontains=query)
            | Q(patient__phone__icontains=query)
            | Q(description__icontains=query)
        )
    pending_bills = pending_bills.order_by("created_at")[:100]
    paid_bills = PatientBill.objects.filter(
        status=PatientBill.Status.PAID,
    ).select_related("patient", "created_by")
    if query:
        paid_bills = paid_bills.filter(
            Q(patient__first_name__icontains=query)
            | Q(patient__second_name__icontains=query)
            | Q(patient__last_name__icontains=query)
            | Q(patient__uhid__icontains=query)
            | Q(patient__phone__icontains=query)
            | Q(description__icontains=query)
        )
    paid_bills = paid_bills.order_by("-paid_at", "-created_at")[:100]
    patients = Patient.objects.none()
    selected_patient = None
    if query:
        patients = Patient.objects.filter(
            Q(first_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(uhid__icontains=query)
            | Q(phone__icontains=query)
        ).order_by("first_name", "last_name")[:50]
    patient_id = request.GET.get("patient", "").strip()
    if patient_id.isdigit():
        selected_patient = Patient.objects.filter(pk=int(patient_id)).first()
    from decimal import Decimal
    selected_patient_net = Decimal("0")
    if selected_patient:
        gross = (selected_patient.reg_fee or Decimal("0")) + (selected_patient.con_fee or Decimal("0"))
        selected_patient_net = max(gross - (selected_patient.discount or Decimal("0")), Decimal("0"))

    selected_bills = []
    selected_paid_total = Decimal("0")
    selected_pending_total = Decimal("0")
    if selected_patient:
        selected_bills = list(
            selected_patient.bills.select_related("created_by").order_by("-created_at")
        )
        for row in selected_bills:
            if row.status == PatientBill.Status.PAID:
                selected_paid_total += row.amount_due
            elif row.status == PatientBill.Status.PENDING:
                selected_pending_total += row.amount_due

    from patients.models import ConsultationSession
    post_consultation_count = (
        PatientBill.objects.filter(
            status=PatientBill.Status.PENDING,
            consultation__isnull=False,
            consultation__status=ConsultationSession.Status.COMPLETED,
            consultation__updated_at__date=timezone.localdate(),
        )
        .values("consultation_id")
        .distinct()
        .count()
    )
    return render(request, "core/bill_command_center.html", {
        "query": query,
        "pending_bills": pending_bills,
        "paid_bills": paid_bills,
        "patients": patients,
        "selected_patient": selected_patient,
        "selected_patient_net": selected_patient_net,
        "selected_bills": selected_bills,
        "selected_bill_count": len(selected_bills),
        "selected_paid_total": selected_paid_total,
        "selected_pending_total": selected_pending_total,
        "post_consultation_count": post_consultation_count,
        "user": request.user,
    })


@login_required(login_url="login")
def billing_console_placeholder(request, console_name, title):
    """Show a shell for billing consoles whose workflow will be defined later."""
    denied = _require_registration_desk(request)
    if denied:
        return denied
    return render(request, "core/billing_console_placeholder.html", {
        "console_name": console_name,
        "title": title,
        "user": request.user,
    })


@login_required(login_url="login")
def patient_bill_slip(request, bill_id):
    """Display a printable pending-payment slip for one patient bill."""
    denied = _require_registration_desk(request)
    if denied:
        return denied
    bill = get_object_or_404(PatientBill.objects.select_related("patient", "created_by"), pk=bill_id)
    return render(request, "core/patient_bill_slip.html", {
        "bill": bill,
        "payment_methods": PatientBill.PaymentMethod.choices,
        "user": request.user,
    })


@login_required(login_url="login")
def bill_set_status(request, bill_id):
    from . import operations_views
    return operations_views.bill_status(request, bill_id)


@login_required(login_url="login")
def post_consultation_billing(request):
    """Registration desk queue for completed consultations with pending bills."""
    denied = _require_registration_desk(request)
    if denied:
        return denied

    from patients.models import ConsultationSession, PatientBill

    today = timezone.localdate()
    query = request.GET.get("q", "").strip()
    pending_bills = PatientBill.objects.filter(
        status=PatientBill.Status.PENDING,
        consultation__isnull=False,
        consultation__updated_at__date=today,
    ).select_related("patient", "consultation__doctor")
    consultation_ids = pending_bills.values_list("consultation_id", flat=True).distinct()
    consultations = (
        ConsultationSession.objects.filter(
            pk__in=consultation_ids,
            status=ConsultationSession.Status.COMPLETED,
            updated_at__date=today,
        )
        .select_related("patient", "doctor", "appointment")
        .prefetch_related("bills", "orders", "pharmacy_issues")
        .order_by("-updated_at")
    )
    if query:
        consultations = consultations.filter(
            Q(patient__first_name__icontains=query)
            | Q(patient__second_name__icontains=query)
            | Q(patient__last_name__icontains=query)
            | Q(patient__uhid__icontains=query)
            | Q(patient__phone__icontains=query)
            | Q(visit_token__icontains=query)
        )
    rows = []
    seen_ids = set()
    for consultation in consultations:
        bills = [bill for bill in consultation.bills.all() if bill.status == PatientBill.Status.PENDING]
        if not bills:
            continue
        service_bills = [bill for bill in bills if bill.source == PatientBill.Source.SERVICE]
        pharmacy_bills = [bill for bill in bills if bill.source == PatientBill.Source.PHARMACY]
        seen_ids.add(consultation.pk)
        rows.append({
            "consultation": consultation,
            "patient": consultation.patient,
            "doctor": consultation.doctor,
            "bills": bills,
            "service_count": len(service_bills),
            "pharmacy_count": len(pharmacy_bills),
            "total": sum((bill.amount_due for bill in bills), 0),
            "pharmacy_prepared": bool(pharmacy_bills),
            "pending_preparation": False,
        })

    # Visits where the doctor released a prescription but pharmacy has not
    # prepared the priced issue yet. Show them so the cashier knows the visit
    # is coming and can coordinate the pharmacy bill.
    preparation = (
        ConsultationSession.objects.filter(
            status=ConsultationSession.Status.COMPLETED,
            updated_at__date=today,
            prescriptions__released_at__isnull=False,
        )
        .exclude(prescriptions__dispense_status="cancelled")
        .exclude(pharmacy_issues__isnull=False)
        .exclude(pk__in=seen_ids)
        .select_related("patient", "doctor", "appointment")
        .distinct()
        .order_by("-updated_at")
    )
    if query:
        preparation = preparation.filter(
            Q(patient__first_name__icontains=query)
            | Q(patient__second_name__icontains=query)
            | Q(patient__last_name__icontains=query)
            | Q(patient__uhid__icontains=query)
            | Q(patient__phone__icontains=query)
            | Q(visit_token__icontains=query)
        )
    for consultation in preparation:
        rows.append({
            "consultation": consultation,
            "patient": consultation.patient,
            "doctor": consultation.doctor,
            "bills": [],
            "service_count": 0,
            "pharmacy_count": 0,
            "total": 0,
            "pharmacy_prepared": False,
            "pending_preparation": True,
        })

    return render(request, "core/post_consultation_billing.html", {
        "rows": rows,
        "query": query,
        "pending_count": len(rows),
        "user": request.user,
    })


@login_required(login_url="login")
def visit_billing(request, consultation_id):
    """Consolidated bill for one consultation: services, pharmacy and doctor charges."""
    denied = _require_registration_desk(request)
    if denied:
        return denied

    from patients.models import ConsultationSession, PatientBill

    consultation = get_object_or_404(
        ConsultationSession.objects.select_related("patient", "doctor", "appointment"),
        pk=consultation_id,
    )
    bills = list(
        consultation.bills.select_related("created_by", "paid_by").order_by(
            "status", "created_at"
        )
    )
    pending_bills = [bill for bill in bills if bill.status == PatientBill.Status.PENDING]
    paid_bills = [bill for bill in bills if bill.status == PatientBill.Status.PAID]
    service_orders = list(
        consultation.orders.select_related("bill", "service_definition", "department").order_by("created_at")
    )
    pharmacy_issues = list(
        consultation.pharmacy_issues.select_related("bill").prefetch_related("lines")
    )
    released_prescriptions = list(
        consultation.prescriptions.filter(released_at__isnull=False)
        .exclude(dispense_status="cancelled")
        .order_by("order_index", "id")
    )
    return render(request, "core/visit_billing.html", {
        "consultation": consultation,
        "patient": consultation.patient,
        "doctor": consultation.doctor,
        "bills": bills,
        "pending_bills": pending_bills,
        "paid_bills": paid_bills,
        "pending_total": sum((bill.amount_due for bill in pending_bills), 0),
        "paid_total": sum((bill.amount_due for bill in paid_bills), 0),
        "service_orders": service_orders,
        "pharmacy_issues": pharmacy_issues,
        "released_prescriptions": released_prescriptions,
        "payment_methods": PatientBill.PaymentMethod.choices,
        "user": request.user,
    })


@require_POST
@login_required(login_url="login")
def visit_billing_pay(request, consultation_id):
    """Collect payment for every pending bill on one consultation."""
    denied = _require_registration_desk(request)
    if denied:
        return denied

    from patients.models import ConsultationSession, PatientBill

    consultation = get_object_or_404(ConsultationSession, pk=consultation_id)
    pending = list(
        consultation.bills.filter(status=PatientBill.Status.PENDING)
    )
    if not pending:
        messages.info(request, "There are no pending bills for this visit.")
        return redirect("visit_billing", consultation_id=consultation.pk)
    try:
        group, paid = pay_bills(
            user=request.user,
            bills=pending,
            payment_method=request.POST.get("payment_method", "cash"),
            payment_reference=request.POST.get("payment_reference", ""),
        )
    except (ValidationError, OperationalError, IntegrityError) as exc:
        messages.error(request, " ".join(exc.messages) if isinstance(exc, ValidationError) else "Payment could not be completed. Refresh and try again.")
        return redirect("visit_billing", consultation_id=consultation.pk)
    messages.success(
        request,
        f"Payment collected for {len(paid)} bill(s). Service and pharmacy work is now released.",
    )
    return redirect("visit_billing_slip", consultation_id=consultation.pk)


@login_required(login_url="login")
def visit_billing_slip(request, consultation_id):
    """Printable consolidated payment slip for one visit."""
    denied = _require_registration_desk(request)
    if denied:
        return denied

    from patients.models import ConsultationSession, PatientBill

    consultation = get_object_or_404(
        ConsultationSession.objects.select_related("patient", "doctor"), pk=consultation_id
    )
    bills = list(
        consultation.bills.select_related("paid_by").order_by("created_at")
    )
    return render(request, "core/visit_billing_slip.html", {
        "consultation": consultation,
        "patient": consultation.patient,
        "doctor": consultation.doctor,
        "bills": bills,
        "total": sum((bill.amount_due for bill in bills if bill.status == PatientBill.Status.PAID), 0),
        "user": request.user,
    })


def _bill_as_whatsapp_text(bill):
    """Format a patient bill as an appealing WhatsApp text message."""
    import re

    from django.utils import timezone

    patient = bill.patient
    age = f"{patient.age} years" if patient.age else "—"
    gender = patient.get_gender_display() or "—"
    created = timezone.localtime(bill.created_at).strftime("%d %b %Y, %I:%M %p")

    lines = [
        "*CAMS Hospital*",
        "",
        f"*Payment Slip {bill.bill_number}*",
        f"Date: {created}",
        "",
        f"*Patient:* {patient.full_name}",
        f"UHID: {patient.uhid or patient.pk}",
        f"Age & Gender: {age} · {gender}",
        f"Phone: {patient.phone or '—'}",
        "",
        f"*Payment details — {bill.get_source_display()}*",
        bill.description,
        "",
    ]
    if bill.source == PatientBill.Source.REGISTRATION:
        lines += [
            f"Registration fee: ₹{patient.reg_fee or 0:,.2f}",
            f"Consultation fee: ₹{patient.con_fee or 0:,.2f}",
            f"Discount: − ₹{bill.discount:,.2f}",
        ]
    lines += [
        f"Amount: ₹{bill.amount:,.2f}",
    ]
    if bill.status == PatientBill.Status.PAID:
        lines.append(f"*Amount paid: ₹{bill.amount_due:,.2f}*")
        lines += ["", "Status: Thank you — this payment has been received."]
    else:
        lines.append(f"*Amount due: ₹{bill.amount_due:,.2f}*")
        lines += ["", "Status: PENDING PAYMENT"]
    lines += [
        "",
        "Thank you for choosing Cams Hospital.",
    ]
    return "\n".join(lines)


@login_required(login_url="login")
def bill_whatsapp(request, bill_id):
    """Open WhatsApp with this bill composed as text for the patient."""
    import re

    denied = _require_registration_desk(request)
    if denied:
        return denied

    from urllib.parse import quote

    bill = get_object_or_404(
        PatientBill.objects.select_related("patient", "created_by"), pk=bill_id
    )

    # Stored phone is normalized to '+91XXXXXXXXXX' by the registration form,
    # so stripping non-digits leaves the country-code format wa.me expects.
    digits = re.sub(r"\D", "", bill.patient.phone or "")
    if not digits:
        messages.error(
            request,
            "This patient has no phone number saved. Add one via Edit Full Details, then send the bill.",
        )
        return redirect("patient_bill_slip", bill_id=bill.pk)

    return redirect(f"https://wa.me/{digits}?text={quote(_bill_as_whatsapp_text(bill))}")


@login_required(login_url="login")
def admin_settings(request):
    """Admin-only settings landing page."""
    denied = _require_admin(request)
    if denied:
        return denied

    return render(request, "core/admin_settings.html")


# ── Doctor / staff management (admin console) ────────────────────────────────

@login_required(login_url="login")
def admin_doctors(request):
    """List and provision staff accounts."""
    denied = _require_admin(request)
    if denied:
        return denied

    from django.contrib.auth.models import User

    from .forms import StaffAccountForm

    if request.method == "POST":
        form = StaffAccountForm(request.POST, request.FILES)
        if form.is_valid():
            user = form.save()
            messages.success(
                request,
                f"Staff account created for {user.get_full_name() or user.username}.",
            )
            return redirect("admin_doctors")
    else:
        form = StaffAccountForm()

    staff_qs = (
        User.objects.select_related("profile")
        .order_by("first_name", "username")
    )

    designation_filter = request.GET.get("designation", "").strip()
    if designation_filter:
        staff_qs = staff_qs.filter(profile__designation=designation_filter)

    search = request.GET.get("q", "").strip()
    if search:
        from django.db.models import Q

        staff_qs = staff_qs.filter(
            Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
        )

    staff_list = [
        {
            "user": user,
            "profile": getattr(user, "profile", None),
            "photo": getattr(getattr(user, "profile", None), "photo", None),
            "initials": ((user.first_name or user.username)[:1] + (user.last_name[:1] if user.last_name else "")).upper(),
            "designation_label": (
                user.profile.get_designation_display()
                if hasattr(user, "profile")
                else "—"
            ),
            "specialty_label": (
                user.profile.get_specialty_display()
                if hasattr(user, "profile") and user.profile.specialty
                else ""
            ),
            "designation": (
                user.profile.designation if hasattr(user, "profile") else ""
            ),
        }
        for user in staff_qs
    ]

    counts = {
        "all": User.objects.count(),
        "doctor": User.objects.filter(profile__designation=UserProfile.Designation.DOCTOR).count(),
        "registration_desk": User.objects.filter(
            profile__designation=UserProfile.Designation.REGISTRATION_DESK
        ).count(),
        "opd_assistant": User.objects.filter(
            profile__designation=UserProfile.Designation.OPD_ASSISTANT
        ).count(),
        "service_staff": User.objects.filter(
            profile__designation=UserProfile.Designation.SERVICE_STAFF
        ).count(),
        "pharmacy": User.objects.filter(
            profile__designation=UserProfile.Designation.PHARMACY
        ).count(),
        "admin": User.objects.filter(profile__designation=UserProfile.Designation.ADMIN).count(),
    }

    return render(request, "core/admin_doctors.html", {
        "form": form,
        "staff_list": staff_list,
        "counts": counts,
        "designation_filter": designation_filter,
        "search": search,
        "designations": UserProfile.Designation.choices,
    })


@login_required(login_url="login")
def admin_doctor_edit(request, pk):
    """Edit a staff account: photo, name, designation, and password."""
    from django.contrib.auth.models import User

    from .forms import StaffAccountForm

    denied = _require_admin(request)
    if denied:
        return denied

    user = get_object_or_404(User, pk=pk)

    if request.method == "POST":
        form = StaffAccountForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            updated = form.save()
            messages.success(
                request,
                f"Updated {updated.get_full_name() or updated.username}.",
            )
            return redirect("admin_doctors")
    else:
        form = StaffAccountForm(instance=user)

    return render(request, "core/admin_doctor_edit.html", {
        "form": form,
        "staff_user": user,
        "profile": getattr(user, "profile", None),
    })


@login_required(login_url="login")
def admin_backup_import(request):
    """Detailed backup and import page for administrators."""
    denied = _require_admin(request)
    if denied:
        return denied

    if request.method == "POST":
        upload = request.FILES.get("backup_file")
        if not upload:
            messages.error(request, "Choose a backup ZIP file to import.")
            return redirect("admin_backup_import")
        if request.POST.get("confirm_import") != "yes":
            messages.error(request, "Confirm the import behavior before continuing.")
            return redirect("admin_backup_import")
        try:
            result = import_patient_backup(upload)
        except BackupArchiveError as exc:
            messages.error(request, str(exc))
        except Exception:
            messages.error(request, "The backup could not be imported. Check the ZIP and try again.")
        else:
            messages.success(
                request,
                f"Import finished: {result['created']} patients added; {result['skipped']} existing patients skipped.",
            )
        return redirect("admin_backup_import")

    return render(request, "core/admin_backup_import.html")


@login_required(login_url="login")
def admin_backup_export(request):
    """Download a ZIP backup of patient records and linked appointments."""
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        backup = build_patient_backup()
    except BackupArchiveError as exc:
        messages.error(request, str(exc))
        return redirect("admin_backup_import")
    filename = f"cams-hms-patient-backup-{date.today():%Y%m%d}.zip"
    return FileResponse(backup, as_attachment=True, filename=filename, content_type="application/zip")


# ── OPD Desk (OPD Assistant portal) ───────────────────────────────────────────

@login_required(login_url="login")
def service_staff_dashboard(request):
    from . import operations_views
    return operations_views.service_dashboard(request)


@login_required(login_url="login")
def pharmacy_dashboard(request):
    from . import operations_views
    return operations_views.pharmacy_dashboard(request)


@login_required(login_url="login")
def pharmacy_stock_report(request):
    from . import operations_views
    return operations_views.pharmacy_stock_report(request)


@login_required(login_url="login")
def pharmacy_dispense_history(request):
    from . import operations_views
    return operations_views.pharmacy_dispense_history(request)

@login_required(login_url="login")
def service_completed_today(request):
    from . import operations_views
    return operations_views.service_completed_today(request)


@login_required(login_url="login")
def opd_inpatient_dashboard(request):
    if _designation_of(request) != UserProfile.Designation.OPD_ASSISTANT:
        raise PermissionDenied
    return render(request, "core/opd_inpatient_dashboard.html")


@login_required(login_url="login")
def opd_assistant_dashboard(request):
    """OPD Central Dashboard with live counts and patient-flow filters."""
    import csv
    from django.http import HttpResponse
    from django.core.paginator import Paginator
    from django.db.models import Q
    from django.utils import timezone
    from patients.models import ClinicalOrder, ConsultationSession, InitialAssessment

    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if not request.user.is_superuser and user_desig != UserProfile.Designation.OPD_ASSISTANT:
        from django.contrib import messages
        messages.warning(request, "Access restricted: the OPD Desk is for OPD Assistants.")
        if user_desig == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        if user_desig == UserProfile.Designation.ADMIN:
            return redirect("admin_dashboard")
        return redirect("dashboard")

    today = timezone.localdate()
    all_patients = list(
        Patient.objects.filter(
            Q(created_at__date=today)
            | Q(appointments__date=today)
            | Q(appointments__created_at__date=today)
            | Q(nurse_tickets__sent_at__date=today) | Q(nurse_tickets__completed_at__date=today)
        ).distinct()
        .select_related("assigned_doctor", "assigned_doctor__profile")
        .prefetch_related(
            "consultations__orders",
            "consultations__prescriptions",
            "consultations__bills",
            "consultations__pharmacy_issues",
            "bills",
            "appointments__bills",
        )
        .order_by("-is_emergency", "created_at")[:500]
    )
    from patients.models import PatientBill
    rows = _opd_patient_flow(all_patients)
    all_rows = rows
    counts = {
        "waiting": sum(row["status"] == "Waiting" for row in rows),
        # The central pool holds every OPD visit for the day.
        "central_pool": len(rows),
        # Patients still at the OPD Desk waiting for their initial assessment
        # (same predicate as the queue's "Awaiting Assessment" tab — needs_doctor
        # mirrors the machine's triage-complete flag).
        "triage_allocation": sum(
            row["pre_visit_paid"]
            and row["consultation"] is None
            and not row["in_nurse_queue"]
            and not row["needs_doctor"]
            for row in rows
        ),
        # Patients whose initial assessment is done and are waiting for a doctor.
        "doctor_allocation": sum(row["status"] == "Doctor Allocation" for row in rows),
        "with_doctors": sum(row["status"] == "With Doctor" for row in rows),
        "investigations": sum(row["status"] == "Investigations" for row in rows),
        # Per-department workload for the service tiles.
        "investigations_lab": sum("laboratory" in row["pending_order_types"] for row in rows),
        "investigations_rad": sum("radiology" in row["pending_order_types"] for row in rows),
        "procedures": sum(row["status"] == "Procedures" for row in rows),
        "pharmacy": sum(row["status"] == "Pharmacy" for row in rows),
        "awaiting_payment": sum(row["status"] == "Awaiting Payment" for row in rows),
        "total": len(rows),
    }

    query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "")
    priority_filter = request.GET.get("priority", "")
    doctor_filter = request.GET.get("doctor", "")
    department_filter = request.GET.get("department", "")
    queue_filter = request.GET.get("queue", "")
    if query:
        rows = [row for row in rows if query.casefold() in " ".join((
            row["patient"].full_name, row["patient"].uhid, row["visit_number"],
        )).casefold()]
    if status_filter:
        rows = [row for row in rows if row["status"] == status_filter]
    if priority_filter:
        rows = [row for row in rows if row["priority"] == priority_filter]
    if doctor_filter:
        rows = [row for row in rows if str(row["doctor_id"] or "") == doctor_filter]
    if department_filter:
        rows = [row for row in rows if row["department"] == department_filter]
    if queue_filter:
        rows = [row for row in rows if queue_filter.casefold() in row["location"].casefold()]

    sort_key = request.GET.get("sort", "arrival")
    sort_fields = {
        "patient": lambda row: row["patient"].full_name.casefold(),
        "status": lambda row: row["status"],
        "arrival": lambda row: row["patient"].created_at,
    }
    rows.sort(key=sort_fields.get(sort_key, sort_fields["arrival"]), reverse=sort_key == "arrival")
    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="opd-patient-flow.csv"'
        writer = csv.writer(response)
        writer.writerow(["UHID", "Visit", "Patient", "Age", "Sex", "Arrival", "Location", "Status", "Doctor", "Pending services", "Alerts", "Billing", "Last update"])
        for row in rows:
            writer.writerow([
                row["patient"].uhid, row["visit_number"], row["patient"].full_name,
                row["patient"].age or "", row["patient"].get_gender_display(),
                row["patient"].created_at.isoformat(), row["location"], row["status"],
                row["doctor"].get_full_name() if row["doctor"] else "",
                ", ".join(row["pending_services"]), row["alert"], row["billing"],
                row["updated_at"].isoformat(),
            ])
        return response
    page_obj = Paginator(rows, 20).get_page(request.GET.get("page"))
    # Roster of doctors with today's assigned-patient counts for the
    # "On-Duty Doctors" card (specialty shown from the profile).
    doctors = (
        get_user_model()
        .objects.filter(profile__designation=UserProfile.Designation.DOCTOR)
        .select_related("profile")
        .annotate(
            patients_today=Count(
                "assigned_patients",
                filter=Q(assigned_patients__created_at__date=today),
                distinct=True,
            )
        )
        .order_by("first_name", "username")
    )
    departments = sorted({row["department"] for row in _opd_patient_flow(all_patients) if row["department"]})
    from patients.models import NurseQueueTicket
    nurse_queue_qs = NurseQueueTicket.objects.filter(status__in=NURSE_OPEN_STATUSES)
    nurse_counts = {
        "queue": nurse_queue_qs.count(),
        "urgent": nurse_queue_qs.filter(
            priority__in=[NurseQueueTicket.Priority.EMERGENCY, NurseQueueTicket.Priority.URGENT]
        ).count(),
        "returned": NurseQueueTicket.objects.filter(
            status=NurseQueueTicket.Status.COMPLETED,
            returned_at__date=today,
        ).count(),
    }

    # Hourly OPD flow for the activity card. Arrivals and consultations both
    # come from the unfiltered pool so the chart stays stable while the table
    # below is filtered.
    from django.utils import timezone as _tz
    hourly_buckets = {h: {"arrivals": 0, "consultations": 0} for h in range(8, 15)}
    for patient in all_patients:
        created = _tz.localtime(patient.created_at)
        if created.date() == today and 8 <= created.hour < 15:
            hourly_buckets[created.hour]["arrivals"] += 1
    for row in all_rows:
        consultation = row.get("consultation")
        if consultation is not None:
            when = getattr(consultation, "updated_at", None) or getattr(consultation, "created_at", None)
            if when is not None:
                when = _tz.localtime(when)
                if when.date() == today and 8 <= when.hour < 15:
                    hourly_buckets[when.hour]["consultations"] += 1
    max_hourly = max(
        (bucket["arrivals"] + bucket["consultations"] for bucket in hourly_buckets.values()),
        default=0,
    ) or 1
    hourly_flow = []
    peak_hour = None
    peak_total = 0
    for hour in range(8, 15):
        bucket = hourly_buckets[hour]
        total = bucket["arrivals"] + bucket["consultations"]
        if total > peak_total:
            peak_total = total
            peak_hour = hour
        height = max(8, round(total * 100 / max_hourly)) if max_hourly else 8
        label = (
            f"{hour}:00 AM" if hour < 12
            else "12:00 PM" if hour == 12
            else f"{hour - 12}:00 PM"
        )
        hourly_flow.append({
            "hour": hour,
            "label": f"{hour:02d}:00",
            "display": label,
            "arrivals": bucket["arrivals"],
            "consultations": bucket["consultations"],
            "total": total,
            "height": height,
            "peak": total == peak_total and total > 0,
        })
    peak_display = (
        f"{peak_hour}:00 AM" if peak_hour is not None and peak_hour < 12
        else "12:00 PM" if peak_hour == 12
        else (f"{peak_hour - 12}:00 PM" if peak_hour is not None else "—")
    )

    from patients.models import NurseQueueTicket as _NQT
    completed_tickets = _NQT.objects.filter(
        status=_NQT.Status.COMPLETED, returned_at__date=today
    )
    triage_speed_count = completed_tickets.count()
    avg_triage_speed = (
        round(sum(t.wait_minutes for t in completed_tickets) / triage_speed_count)
        if triage_speed_count
        else 0
    )
    workflow_active = bool(all_rows)
    from django.db.models import Case, When, Value, IntegerField, F
    from patients.models import NurseTask
    nurse_open = _NQT.objects.filter(status__in=NURSE_OPEN_STATUSES)
    nurse_waiting = nurse_open.filter(status__in=[_NQT.Status.WAITING, _NQT.Status.RECALLED])
    open_tasks = NurseTask.objects.filter(status__in=[NurseTask.Status.PENDING, NurseTask.Status.IN_PROGRESS])
    now = timezone.now()
    nursing_counts = {
        "awaiting": nurse_waiting.count(),
        "assessing": nurse_open.filter(status=_NQT.Status.IN_ASSESSMENT).count(),
        "urgent": nurse_open.filter(priority__in=[_NQT.Priority.EMERGENCY, _NQT.Priority.URGENT]).count(),
        "tasks": open_tasks.count(),
    }
    assessment_preview = list(nurse_open.select_related("patient", "claimed_by").annotate(
        priority_order=Case(When(priority=_NQT.Priority.EMERGENCY, then=Value(0)),
                            When(priority=_NQT.Priority.URGENT, then=Value(1)),
                            default=Value(2), output_field=IntegerField())
    ).order_by("priority_order", "sent_at", "pk")[:8])
    task_preview = list(open_tasks.filter(Q(priority=NurseTask.Priority.URGENT) | Q(due_at__lt=now))
        .select_related("patient", "assigned_to").annotate(
            attention_order=Case(When(due_at__lt=now, then=Value(0)),
                                 default=Value(1), output_field=IntegerField()))
        .order_by("attention_order", F("due_at").asc(nulls_last=True), "created_at", "pk")[:6])

    return render(request, "core/opd_assistant_dashboard.html", {
        "today": today,
        "counts": counts,
        "page_obj": page_obj,
        "doctors": doctors,
        "departments": departments,
        "query": query,
        "status_filter": status_filter,
        "priority_filter": priority_filter,
        "doctor_filter": doctor_filter,
        "department_filter": department_filter,
        "queue_filter": queue_filter,
        "nurse_counts": nurse_counts,
        "nursing_counts": nursing_counts,
        "assessment_preview": assessment_preview,
        "task_preview": task_preview,
        "dashboard_loaded_at": now,
        "hourly_flow": hourly_flow,
        "peak_display": peak_display,
        "avg_triage_speed": avg_triage_speed,
        "workflow_active": workflow_active,
        "user": request.user,
    })


def _opd_patient_flow(patients):
    """Build patient-flow rows from the existing persisted OPD records."""
    from decimal import Decimal

    from appointments.models import Appointment
    from patients.models import ConsultationSession, InitialAssessment

    today = timezone.localdate()
    rows = []
    for patient in patients:
        assessment = getattr(patient, "initial_assessment", None)
        consultations = list(patient.consultations.all())
        consultation = consultations[0] if consultations else None
        assigned_doctor = patient.assigned_doctor or (consultation.doctor if consultation else None)
        orders = [order for item in consultations for order in item.orders.all()]
        prescriptions = [prescription for item in consultations for prescription in item.prescriptions.all() if prescription.released_at and prescription.dispense_status != "cancelled"]
        pending_orders = [order for order in orders if order.status.casefold() not in ("completed", "cancelled")]
        latest_bill = patient.bills.all().order_by("-created_at").first()
        # Booked appointments surface as pending-assignment pool entries so the
        # OPD assistant can route them to a doctor before the visit.
        next_appointment = next(
            (
                appt
                for appt in patient.appointments.all()
                if appt.status == Appointment.Status.SCHEDULED and appt.date >= today
            ),
            None,
        )
        if next_appointment is None and consultation is not None:
            next_appointment = consultation.appointment
        #: Open nurse ticket — set only when the visit was actually routed to the
        #: nurse desk (or auto-routed by the emergency intake), so ordinary
        #: walk-ins without vitals keep their existing flow.
        nurse_ticket = _nurse_ticket_for_patient(patient)
        triage_pending = bool(nurse_ticket) and consultation is None
        completed_current_ticket = None
        if next_appointment is not None:
            completed_current_ticket = (
                patient.nurse_tickets.filter(
                    appointment=next_appointment,
                    status="completed",
                )
                .order_by("-completed_at")
                .first()
            )
        pre_visit_pending = (
            appointment_pre_visit_pending(next_appointment)
            and not patient.is_emergency
        )
        pre_visit_paid = not pre_visit_pending
        # Mirror the assign_doctor gate (a completed triage ticket for the
        # active visit, or an assessment completed today) so "Doctor Allocation"
        # rows are always actually assignable — including assessments recorded
        # from the shared workstation that never opened a triage ticket.
        assessment_today = bool(
            assessment is not None
            and assessment.status == InitialAssessment.Status.COMPLETED
            and assessment.completed_at is not None
            and timezone.localtime(assessment.completed_at).date() == today
        )
        triage_complete_for_visit = bool(completed_current_ticket) or assessment_today
        post_visit_pending = False
        post_visit_total = Decimal("0")
        if (
            consultation is not None
            and consultation.status == ConsultationSession.Status.COMPLETED
        ):
            pending_bills = consultation_pending_bills(consultation)
            post_visit_pending = pending_bills.exists()
            post_visit_total = sum(
                (bill.amount_due for bill in pending_bills), Decimal("0")
            )

        if consultation and consultation.status == ConsultationSession.Status.CANCELLED:
            status, location = "Cancelled", "Visit cancelled"
        elif pre_visit_pending:
            status, location = "Awaiting Payment", "Registration Desk"
        elif post_visit_pending:
            status, location = "Awaiting Payment", "Cashier"
        elif patient.diagnostic_reports.filter(published_at__isnull=False, reviewed_at__isnull=True).exists() or any(order.status.casefold() in ("review required", "review") for order in orders):
            status, location = "Review Required", "Doctor Review"
        elif triage_pending:
            # Waiting in the triage workstation for vitals / initial assessment.
            status, location = "Waiting", "Assessment"
        elif any(order.order_type == "laboratory" for order in pending_orders):
            status, location = "Investigations", "Laboratory"
        elif any(order.order_type == "radiology" for order in pending_orders):
            status, location = "Investigations", "Radiology"
        elif any(order.order_type == "other" for order in pending_orders):
            status, location = "Procedures", "Service Queue"
        elif patient.bills.filter(status="pending", pharmacy_issue__status="awaiting_payment").exists():
            status, location = "Awaiting Payment", "Pharmacy"
        elif any(item.dispense_status != "dispensed" for item in prescriptions):
            status, location = "Pharmacy", "Pharmacy"
        elif consultation and consultation.status == ConsultationSession.Status.COMPLETED:
            status, location = "Completed", "Completed"
        elif consultation and consultation.status == ConsultationSession.Status.IN_PROGRESS:
            status, location = "With Doctor", "Doctor Consultation"
        elif triage_complete_for_visit:
            status, location = "Doctor Allocation", "Doctor Queue"
        elif consultation:
            status, location = "With Doctor", "Doctor Consultation"
        else:
            # Every visit reaching this point is paid and pre-consultation —
            # the initial assessment is the next step at the OPD Desk.
            status, location = "Assessment", "OPD Desk"
        allergies = bool(patient.allergies or (assessment and assessment.allergies))
        pending_due = post_visit_total
        if pre_visit_pending:
            pending_due = sum(
                (bill.amount_due for bill in appointment_pre_visit_bills(next_appointment)),
                Decimal("0"),
            )
        rows.append({
            "patient": patient,
            "assessment": assessment,
            # Open nurse ticket (None once the assessment is saved back).
            "nurse_ticket": nurse_ticket,
            "in_nurse_queue": bool(nurse_ticket),
            "pre_visit_pending": pre_visit_pending,
            "pre_visit_paid": pre_visit_paid,
            "post_visit_pending": post_visit_pending,
            "post_visit_total": post_visit_total,
            "payment_due": pending_due,
            "needs_doctor": (
                pre_visit_paid
                and consultation is None
                and triage_complete_for_visit
            ),
            "consultation": consultation,
            "appointment": next_appointment,
            "visit_number": consultation.visit_token if consultation else f"OPD-{patient.created_at:%y%m%d}-{patient.pk:04d}",
            "status": status,
            "location": location,
            "priority": "Urgent" if patient.is_emergency else "Routine",
            "doctor": assigned_doctor,
            "doctor_id": assigned_doctor.pk if assigned_doctor else None,
            "department": (
                assigned_doctor.profile.get_specialty_display()
                if assigned_doctor and assigned_doctor.profile.specialty
                else "General Medicine"
            ),
            "pending_services": [order.name for order in pending_orders] + [
                item.medicine_name for item in prescriptions if item.dispense_status != "dispensed"
            ],
            "pending_order_types": sorted({order.order_type for order in pending_orders}),
            "alert": "Emergency" if patient.is_emergency else ("Allergy on file" if allergies else ""),
            "payment_label": (
                "Registration payment pending"
                if pre_visit_pending
                else "Post-consultation payment pending"
                if post_visit_pending
                else "Emergency — collect later"
                if patient.is_emergency
                and latest_bill is not None
                and latest_bill.status == "pending"
                else "Clear for current stage"
            ),
            "billing": (
                f"Pending ₹{pending_due:,.2f}"
                if pending_due
                else (latest_bill.get_status_display() if latest_bill else "Unbilled")
            ),
            "updated_at": max(
                [patient.updated_at] + ([assessment.updated_at] if assessment else [])
                + ([consultation.updated_at] if consultation else [])
            ),
        })
    return rows


@login_required(login_url="login")
def opd_patient_queue(request):
    """Central patient queue with a selected visit summary."""
    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if not request.user.is_superuser and designation != UserProfile.Designation.OPD_ASSISTANT:
        messages.warning(request, "Access restricted: the Patient Queue is for OPD Assistants.")
        return redirect("home")
    from django.utils import timezone
    today = timezone.localdate()
    patients = list(
        Patient.objects.filter(
            Q(created_at__date=today)
            | Q(appointments__date=today)
            | Q(appointments__created_at__date=today)
            | Q(nurse_tickets__sent_at__date=today) | Q(nurse_tickets__completed_at__date=today)
        ).distinct()
        .select_related("assigned_doctor", "assigned_doctor__profile")
        .prefetch_related(
            "consultations__orders",
            "consultations__prescriptions",
            "consultations__bills",
            "consultations__pharmacy_issues",
            "bills",
            "appointments__bills",
        )
        .order_by("-is_emergency", "created_at")
    )
    all_rows = _opd_patient_flow(patients)
    query = request.GET.get("q", "").strip()
    allocation_filter = (request.GET.get("allocation") or "all").strip().lower()
    service_filter = (request.GET.get("service") or "all").strip().lower()

    def _needs_nurse(row):
        # Same predicate as the dashboard's "Awaiting Assessment" KPI: paid,
        # no consultation, not in triage, assessment gate not yet satisfied
        # (needs_doctor mirrors the machine's triage-complete flag).
        if not row["pre_visit_paid"]:
            return False
        if row["in_nurse_queue"] or row["consultation"] is not None:
            return False
        return not row["needs_doctor"] and row["status"] not in ("Completed", "Cancelled")

    service_map = {
        "laboratory": "Laboratory",
        "radiology": "Radiology",
        "pharmacy": "Pharmacy",
        "procedure": "Service",
    }

    rows = list(all_rows)
    if query:
        rows = [row for row in rows if query.casefold() in " ".join((
            row["patient"].full_name, row["patient"].uhid, row["visit_number"],
        )).casefold()]
    if allocation_filter == "nurse":
        rows = [row for row in rows if _needs_nurse(row)]
    elif allocation_filter == "doctor":
        rows = [row for row in rows if row.get("needs_doctor")]
    elif allocation_filter == "services":
        rows = [row for row in rows if row["status"] in ("Investigations", "Procedures", "Pharmacy")]
    elif allocation_filter == "payment":
        rows = [row for row in rows if row["status"] == "Awaiting Payment"]
    elif allocation_filter == "central":
        rows = list(rows)
    if service_filter in service_map:
        target = service_map[service_filter].casefold()
        rows = [row for row in rows if target in (row["location"] or "").casefold()]

    selected_id = request.GET.get("patient", "")
    selected = next((row for row in rows if str(row["patient"].pk) == selected_id), None)
    if selected is None and selected_id:
        selected = next((row for row in all_rows if str(row["patient"].pk) == selected_id), None)

    # Journey-card extras for the selected visit: the six-step journey rail,
    # vitals alerts and the triage score band (all reused helpers).
    if selected is not None:
        from patients.models import ConsultationSession as _CS, InitialAssessment as _IA

        assessment = selected.get("assessment")
        consultation = selected.get("consultation")
        assessed_done = bool(selected["needs_doctor"]) or consultation is not None or bool(
            assessment is not None and assessment.status == _IA.Status.COMPLETED
        )
        doctor_note = ""
        if selected["doctor"] is not None and not selected["needs_doctor"]:
            doctor_note = "Dr. " + (selected["doctor"].get_full_name() or selected["doctor"].username)
        selected["journey"] = [
            {
                "label": "Registered",
                "state": "done",
                "time": selected["patient"].created_at,
                "note": "",
            },
            {
                "label": "Paid",
                "state": "done" if selected["pre_visit_paid"] else "current",
                "time": None,
                "note": (
                    "collect later"
                    if selected["patient"].is_emergency and not selected["pre_visit_paid"]
                    else ""
                ),
            },
            {
                "label": "Assessment",
                "state": "done" if assessed_done else ("current" if selected["pre_visit_paid"] else "todo"),
                "time": assessment.completed_at if assessed_done and assessment is not None else None,
                "note": "",
            },
            {
                "label": "Doctor",
                "state": (
                    "done"
                    if consultation is not None or (selected["doctor"] is not None and not selected["needs_doctor"])
                    else "current" if selected["needs_doctor"]
                    else "todo"
                ),
                "time": None,
                "note": doctor_note,
            },
            {
                "label": "Consultation",
                "state": (
                    "done"
                    if consultation is not None and consultation.status == _CS.Status.COMPLETED
                    else "current" if consultation is not None
                    else "todo"
                ),
                "time": consultation.updated_at if consultation is not None else None,
                "note": "",
            },
            {
                "label": "Completed",
                "state": (
                    "done" if selected["status"] == "Completed"
                    else "current" if selected["status"] in (
                        "Investigations", "Procedures", "Pharmacy", "Review Required",
                    )
                    else "todo"
                ),
                "time": consultation.updated_at if consultation is not None and selected["status"] == "Completed" else None,
                "note": "",
            },
        ]
        if assessment is not None:
            score = _triage_priority_score(assessment)
            selected["priority_score"] = score
            selected["priority_band"] = _priority_band(score)
            selected["vitals_alerts"] = _nurse_vitals_alerts(assessment)
        else:
            selected["priority_score"] = 0
            selected["priority_band"] = _priority_band(0)
            selected["vitals_alerts"] = []

    from django.core.paginator import Paginator
    page_obj = Paginator(rows, 20).get_page(request.GET.get("page"))
    doctors = (
        get_user_model()
        .objects.filter(profile__designation=UserProfile.Designation.DOCTOR)
        .select_related("profile")
        .annotate(
            patients_today=Count(
                "assigned_patients",
                filter=Q(assigned_patients__created_at__date=today),
                distinct=True,
            )
        )
        .order_by("first_name", "username")
    )

    allocation_counts = {
        "all": len(all_rows),
        "nurse": sum(1 for row in all_rows if _needs_nurse(row)),
        "doctor": sum(1 for row in all_rows if row.get("needs_doctor")),
        "services": sum(1 for row in all_rows if row["status"] in ("Investigations", "Procedures", "Pharmacy")),
        "payment": sum(1 for row in all_rows if row["status"] == "Awaiting Payment"),
    }
    return render(request, "core/opd_workspace.html", {
        "page_title": "Patient Queue",
        "page_kind": "patient_queue",
        "rows": page_obj,
        "selected": selected,
        "query": query,
        "doctors": doctors,
        "today": today,
        "allocation_filter": allocation_filter,
        "service_filter": service_filter,
        "allocation_counts": allocation_counts,
        "selected_reports": accessible_reports(request.user).filter(patient=selected["patient"]) if selected else [],
    })


def _is_hospital_admin(request, designation=None):
    if designation is None:
        designation = _designation_of(request)
    return request.user.is_superuser or designation == UserProfile.Designation.ADMIN


@login_required(login_url="login")
@require_POST
def opd_order_update(request, order_id):
    from . import operations_views
    return operations_views.service_order_update(request, order_id)


@login_required(login_url="login")
def opd_service_queue(request, service):
    from . import operations_views
    return operations_views.service_queue(request, service)


@login_required(login_url="login")
@require_POST
def opd_prescription_update(request, item_id):
    from . import operations_views
    return operations_views.legacy_dispense_update(request, item_id)


@login_required(login_url="login")
def opd_module_page(request, module):
    """Role-scoped supporting pages for the OPD Assistant portal."""
    import csv
    from django.http import HttpResponse
    from patients.models import ClinicalOrder, OPDAuditEvent

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if not request.user.is_superuser and designation != UserProfile.Designation.OPD_ASSISTANT:
        messages.warning(request, "Access restricted: this page is for OPD Assistants.")
        return redirect("home")
    titles = {
        "reports": "OPD Reports",
        "master-data": "Master Data",
        "audit-trail": "Audit Trail",
        "settings": "OPD Settings",
    }
    if module not in titles:
        from django.http import Http404
        raise Http404

    if module == "settings" and request.method == "POST":
        request.session["opd_preferences"] = {
            "notifications": request.POST.get("notifications") == "on",
            "compact_tables": request.POST.get("compact_tables") == "on",
        }
        messages.success(request, "OPD display preferences saved for this signed-in session.")
        return redirect("opd_module", module="settings")

    patients = Patient.objects.all().select_related("assigned_doctor", "assigned_doctor__profile")
    orders = ClinicalOrder.objects.select_related("consultation__patient", "consultation__doctor")
    start_date = request.GET.get("from", "")
    end_date = request.GET.get("to", "")
    doctor_id = request.GET.get("doctor", "")
    service = request.GET.get("service", "")
    if start_date:
        patients = patients.filter(created_at__date__gte=start_date)
        orders = orders.filter(created_at__date__gte=start_date)
    if end_date:
        patients = patients.filter(created_at__date__lte=end_date)
        orders = orders.filter(created_at__date__lte=end_date)
    if doctor_id.isdigit():
        patients = patients.filter(assigned_doctor_id=doctor_id)
        orders = orders.filter(consultation__doctor_id=doctor_id)
    if service:
        orders = orders.filter(order_type=service)

    if module == "reports" and request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="opd-report.csv"'
        writer = csv.writer(response)
        writer.writerow(["Patient", "UHID", "Visit", "Service", "Status", "Doctor", "Ordered at"])
        for order in orders.order_by("-created_at")[:1000]:
            writer.writerow([
                order.consultation.patient.full_name,
                order.consultation.patient.uhid,
                order.consultation.visit_token,
                order.name,
                order.status,
                order.consultation.doctor.get_full_name() if order.consultation.doctor else "",
                order.created_at.isoformat(),
            ])
        return response

    context = {
        "page_title": titles[module],
        "module": module,
        "doctors": get_user_model().objects.filter(profile__designation=UserProfile.Designation.DOCTOR).order_by("first_name", "username"),
        "patients": patients.order_by("-created_at")[:100],
        "orders": orders.order_by("-created_at")[:100],
        "start_date": start_date,
        "end_date": end_date,
        "doctor_filter": doctor_id,
        "service_filter": service,
        "services": ClinicalOrder.OrderType.choices,
        "audit_events": OPDAuditEvent.objects.select_related("patient", "actor", "order").order_by("-occurred_at")[:200],
        "opd_preferences": request.session.get("opd_preferences", {"notifications": True, "compact_tables": False}),
    }
    return render(request, "core/opd_module.html", context)


@login_required(login_url="login")
def opd_referrals(request):
    """Create and review internal or external OPD referrals."""
    import os
    from django.db.models import Q
    from django.urls import reverse
    from patients.models import OPDAuditEvent, OPDReferral, ConsultationSession

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if not request.user.is_superuser and designation not in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.DOCTOR,
    ):
        messages.warning(request, "Access restricted: referrals are for OPD Assistants and Doctors.")
        return redirect("home")

    if request.method == "POST":
        patient = Patient.objects.filter(pk=request.POST.get("patient_id", "")).first()
        department = request.POST.get("department", "").strip()[:100]
        reason = request.POST.get("reason", "").strip()[:255]
        referral_type = request.POST.get("referral_type", OPDReferral.ReferralType.INTERNAL)
        urgency = request.POST.get("urgency", OPDReferral.Urgency.ROUTINE)
        action = request.POST.get("action", "draft")
        attachment = request.FILES.get("attachment")
        doctor_id = request.POST.get("doctor_id", "").strip()
        doctor = get_user_model().objects.filter(
            pk=doctor_id, profile__designation=UserProfile.Designation.DOCTOR
        ).first() if doctor_id.isdigit() else None
        allowed_extensions = {".pdf", ".png", ".jpg", ".jpeg"}
        if not patient or not department or not reason:
            messages.error(request, "Select a patient, destination department, and referral reason.")
        elif referral_type not in OPDReferral.ReferralType.values or urgency not in OPDReferral.Urgency.values:
            messages.error(request, "Choose a valid referral type and urgency.")
        elif action not in ("draft", "send"):
            messages.error(request, "Choose save draft or send referral.")
        elif doctor_id and not doctor:
            messages.error(request, "Select an available doctor account.")
        elif attachment and (
            attachment.size > 5 * 1024 * 1024
            or os.path.splitext(attachment.name)[1].lower() not in allowed_extensions
        ):
            messages.error(request, "Attachment must be PDF, PNG, or JPG and no larger than 5 MB.")
        else:
            consultation = ConsultationSession.objects.filter(
                patient=patient,
                status__in=[ConsultationSession.Status.DRAFT, ConsultationSession.Status.IN_PROGRESS],
            ).first()
            referral = OPDReferral.objects.create(
                patient=patient,
                consultation=consultation,
                referral_type=referral_type,
                department=department,
                doctor=doctor,
                reason=reason,
                urgency=urgency,
                notes=request.POST.get("notes", "").strip(),
                attachment=attachment,
                status=OPDReferral.Status.SENT if action == "send" else OPDReferral.Status.DRAFT,
                created_by=request.user,
            )
            OPDAuditEvent.objects.create(
                patient=patient,
                actor=request.user,
                action="referral_sent" if action == "send" else "referral_draft_saved",
                summary=f"{referral.get_status_display()}: {referral.department} referral ({referral.urgency})",
                details={"referral_id": referral.pk, "department": department, "urgency": urgency},
            )
            messages.success(request, "Referral sent to the destination queue." if action == "send" else "Referral draft saved.")
            return redirect(f"{reverse('opd_referrals')}?patient={patient.pk}")

    from django.db.models import Count
    from django.utils import timezone
    today = timezone.localdate()
    query = request.GET.get("q", "").strip()
    patient_fields = (
        "id", "first_name", "last_name", "uhid", "date_of_birth",
        "gender", "is_emergency", "created_at", "allergies", "phone",
    )
    # Directory: emergencies pinned on top, then newest first, with the
    # patient's referral history count attached for the context chips.
    patients = (
        Patient.objects.only(*patient_fields)
        .annotate(referral_count=Count("opd_referrals"))
        .order_by("-is_emergency", "-created_at")
    )
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query) | Q(last_name__icontains=query)
            | Q(second_name__icontains=query) | Q(uhid__icontains=query)
            | Q(phone__icontains=query)
        )
    patient_id = request.GET.get("patient", "")
    selected_patient = (
        Patient.objects.only(*patient_fields).filter(pk=patient_id).first()
        if patient_id.isdigit() else None
    )
    referrals = (
        OPDReferral.objects.select_related("patient", "doctor", "created_by")
        .only(
            "id", "department", "reason", "urgency", "status", "created_at",
            "attachment", "patient__id", "patient__first_name", "patient__last_name",
            "patient__uhid", "doctor__id", "doctor__first_name", "doctor__last_name",
            "doctor__username", "created_by__id", "created_by__first_name",
            "created_by__last_name", "created_by__username",
        )
        .order_by("-created_at")
    )
    if selected_patient:
        referrals = referrals.filter(patient=selected_patient)

    referral_stats = OPDReferral.objects.aggregate(
        total=Count("id"),
        urgent=Count("id", filter=Q(urgency=OPDReferral.Urgency.URGENT)),
        pending=Count("id", filter=Q(status__in=[
            OPDReferral.Status.DRAFT, OPDReferral.Status.SENT,
        ])),
    )

    # Paginated directory (30 per page) with an accurate showing range.
    from django.core.paginator import Paginator
    page_obj = Paginator(patients, 30).get_page(request.GET.get("page"))
    directory_total = page_obj.paginator.count
    directory_emergency = patients.filter(is_emergency=True).count()
    showing_from = page_obj.start_index() if directory_total else 0
    showing_to = page_obj.end_index() if directory_total else 0

    return render(request, "core/opd_referrals.html", {
        "page_title": "Cross-consultation / Referral",
        "today": today,
        "page_obj": page_obj,
        "patient_total": Patient.objects.count(),
        "directory_total": directory_total,
        "directory_emergency": directory_emergency,
        "showing_from": showing_from,
        "showing_to": showing_to,
        "selected_patient": selected_patient,
        "referrals": referrals[:50],
        "referral_stats": referral_stats,
        "doctors": get_user_model().objects.filter(
            profile__designation=UserProfile.Designation.DOCTOR
        ).select_related("profile").order_by("first_name", "username"),
        "query": query,
        "referral_types": OPDReferral.ReferralType.choices,
        "urgencies": OPDReferral.Urgency.choices,
    })


# ── Registration Desk ─────────────────────────────────────────────────────────

@login_required(login_url="login")
def reception_dashboard(request):
    """Dedicated dashboard for registration desk."""
    # Block Doctor/OPD Assistant users from accessing registration desk dashboard
    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig in ["doctor", "opd_assistant"] and not request.user.is_superuser:
        from django.contrib import messages
        if user_desig == "opd_assistant":
            messages.warning(request, "Access restricted: OPD Assistants use the OPD Desk portal.")
            return redirect("opd_assistant_dashboard")
        messages.warning(request, "Access restricted: Doctor users should use the Doctor Dashboard.")
        return redirect("dashboard")

    today = date.today()
    todays_patients = Patient.objects.filter(created_at__date=today).order_by("-created_at")
    recent_patients = Patient.objects.order_by("-created_at")[:12]
    total_patients = Patient.objects.count()
    emergency_today = Patient.objects.filter(created_at__date=today, is_emergency=True).count()

    # Billing: bills still awaiting payment.
    from decimal import Decimal

    from patients.models import PatientBill
    pending_bills = PatientBill.objects.filter(status=PatientBill.Status.PENDING)
    pending_count = pending_bills.count()
    pending_due = sum((b.amount_due for b in pending_bills), Decimal("0"))

    paid_today_bills = PatientBill.objects.filter(created_at__date=today, status=PatientBill.Status.PAID)
    paid_count = paid_today_bills.count()
    paid_today_amount = sum((b.amount_due for b in paid_today_bills), Decimal("0"))

    # Appointments expected at the counter today (cancelled slots excluded).
    todays_appointments_qs = Appointment.objects.filter(date=today).exclude(
        status=Appointment.Status.CANCELLED
    ).select_related("patient", "doctor").order_by("start_time")
    appointments_today = todays_appointments_qs.count()
    upcoming_appointments = list(todays_appointments_qs[:5])

    # Available doctors for counter triage and allocation
    available_doctors = Doctor.objects.all().order_by("first_name", "last_name")[:6]

    from django.db.models import Q as _Q
    flow_patients = list(
        Patient.objects.filter(
            _Q(created_at__date=today) | _Q(appointments__date=today)
        )
        .distinct()
        .select_related("assigned_doctor", "assigned_doctor__profile")
        .prefetch_related(
            "consultations__orders",
            "consultations__prescriptions",
            "consultations__bills",
            "consultations__pharmacy_issues",
            "bills",
            "appointments__bills",
        )
    )
    flow_rows = _opd_patient_flow(flow_patients)
    ready_for_opd_count = sum(
        row["status"] == "Assessment" for row in flow_rows
    )
    from patients.models import ConsultationSession
    post_consultation_count = (
        PatientBill.objects.filter(
            status=PatientBill.Status.PENDING,
            consultation__status=ConsultationSession.Status.COMPLETED,
            consultation__updated_at__date=today,
        )
        .values("consultation_id")
        .distinct()
        .count()
    )

    return render(request, "core/reception_dashboard.html", {
        "today": today,
        "total_patients": total_patients,
        "today_count": todays_patients.count(),
        "todays_patients": todays_patients,
        "recent_patients": recent_patients,
        "pending_payments": pending_count,
        "pending_amount": pending_due,
        "paid_count": paid_count,
        "paid_today_amount": paid_today_amount,
        "emergency_today": emergency_today,
        "appointments_today": appointments_today,
        "upcoming_appointments": upcoming_appointments,
        "available_doctors": available_doctors,
        "ready_for_opd_count": ready_for_opd_count,
        "post_consultation_count": post_consultation_count,
        "user": request.user,
    })


@login_required(login_url="login")
def registration_desk(request):
    """Registration desk hub — search the register, start a new registration and
    review past records.

    This page now carries the functionality that used to live on the separate
    History screen: a search bar over the patient register, a shortcut to the
    new-registration form, and the full record list (filter, edit, delete).
    """
    from datetime import datetime

    designation = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    is_registration_desk = designation == UserProfile.Designation.REGISTRATION_DESK
    can_register = is_registration_desk or request.user.is_superuser

    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    date_str = request.GET.get("date", "").strip()
    selected_date = None
    if date_str:
        try:
            selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            selected_date = None

    rows = _history_rows(
        query=query,
        status=status,
        selected_date=selected_date,
        include_appointments=True,
    )

    recent_registered = Patient.objects.order_by("-created_at")[:5]

    return render(request, "core/registration_desk.html", {
        "rows": rows,
        "row_count": len(rows),
        "query": query,
        "status": status,
        "selected_date": selected_date,
        "date_str": date_str,
        "status_choices": Appointment.Status.choices,
        "is_registration_desk": is_registration_desk,
        "can_register": can_register,
        "recent_registered": recent_registered,
        "last_registered": recent_registered[0] if recent_registered else None,
        "user": request.user,
    })


@login_required(login_url="login")
def registration_desk_new(request):
    """New patient registration form — Registration Desk counter."""
    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig == "doctor" and not request.user.is_superuser:
        from django.contrib import messages
        messages.warning(request, "Access restricted: Patient Registration counter is for Registration Desk staff only.")
        return redirect("dashboard")
    from decimal import Decimal

    from patients.models import Patient

    # Compute next UHID (simple auto-increment)
    last = Patient.objects.order_by("-id").first()
    next_mr = (int(last.uhid) + 1) if (last and last.uhid and last.uhid.isdigit()) else 100001

    if request.method == "POST":
        from patients.forms import PatientRegistrationForm
        form = PatientRegistrationForm(request.POST, request.FILES)
        if form.is_valid():
            with transaction.atomic():
                last_locked = Patient.objects.select_for_update().order_by("-id").first()
                current_mr = (int(last_locked.uhid) + 1) if (last_locked and last_locked.uhid and last_locked.uhid.isdigit()) else 100001
                patient = form.save(commit=False)
                patient.uhid = str(current_mr)

                # If photo was captured directly via webcam
                photo_data = request.POST.get("photo_data", "").strip()
                if photo_data and photo_data.startswith("data:image"):
                    import base64
                    from django.core.files.base import ContentFile
                    try:
                        format_header, imgstr = photo_data.split(";base64,")
                        ext = "jpg"
                        if "png" in format_header:
                            ext = "png"
                        elif "webp" in format_header:
                            ext = "webp"
                        clean_name = patient.first_name.lower().replace(" ", "_") or "patient"
                        filename = f"patient_{current_mr}_{clean_name}.{ext}"
                        patient.photo.save(filename, ContentFile(base64.b64decode(imgstr)), save=False)
                    except Exception:
                        pass

                patient.save()

                # New registrations automatically generate an OPD appointment. No
                # doctor is chosen here; the OPD assistant assigns one from the
                # central pool.
                from django.utils import timezone as _tz
                from appointments.models import Appointment as _Appointment
                appointment = _Appointment.objects.create(
                    patient=patient,
                    doctor=None,
                    date=_tz.localdate(),
                    start_time=__import__("datetime").datetime.now().time().replace(second=0, microsecond=0),
                    duration_minutes=30,
                    status=_Appointment.Status.SCHEDULED,
                    reason="New patient registration - OPD",
                )

                registration_total = (patient.reg_fee or Decimal("0")) + (patient.con_fee or Decimal("0"))
                registration_discount = min(patient.discount or Decimal("0"), registration_total)
                if registration_total - registration_discount > 0:
                    PatientBill.objects.create(
                        patient=patient,
                        source=PatientBill.Source.REGISTRATION,
                        appointment=appointment,
                        description="Patient registration & consultation fee",
                        amount=registration_total,
                        discount=registration_discount,
                        created_by=request.user,
                    )
            return redirect("patient_print_card", patient_id=patient.pk)
    else:
        from patients.forms import PatientRegistrationForm
        form = PatientRegistrationForm(initial={
            "nationality": "India",
            "state": "Bihar",
            "patient_category": "GENERAL",
            "billing_type": "Individual",
            "payment_type": "cash",
        })

    from datetime import date as dt
    today = dt.today()
    recent_registered = Patient.objects.order_by("-created_at")[:5]

    return render(request, "core/registration_desk_form.html", {
        "form": form,
        "next_mr": next_mr,
        "today": today,
        "recent_registered": recent_registered,
        "last_registered": recent_registered[0] if recent_registered else None,
        "user": request.user,
        "fee_configuration_missing": form.default_fee is None and not any(value is not None for value in form.category_fees.values()),
        "category_fees": form.category_fees,
        "default_registration_fee": form.default_fee,
        "default_consultation_fee": form.default_consultation_fee,
        "default_discount": form.default_discount,
    })


@login_required(login_url="login")
def emergency_registration(request):
    """Fast emergency intake counter.

    Registers a patient with only the essentials (name, age, sex, UHID),
    flags the record as an emergency, and raises a PENDING registration-fee bill
    so the payment is collected later from the Bill Command Center. The remaining
    registration details are completed later by editing the record from History.
    """
    from decimal import Decimal

    from django.utils import timezone

    from patients.forms import EmergencyRegistrationForm
    from patients.models import RegistrationFeeSettings

    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig == "doctor" and not request.user.is_superuser:
        messages.warning(request, "Access restricted: Emergency registration is for Registration Desk staff only.")
        return redirect("dashboard")

    def _next_mr():
        last = Patient.objects.order_by("-id").first()
        if last and last.uhid and last.uhid.isdigit():
            return int(last.uhid) + 1
        return 100001

    next_mr = _next_mr()
    default_fee = (
        RegistrationFeeSettings.objects.order_by("pk")
        .values_list("default_registration_fee", flat=True)
        .first()
    )
    default_consultation_fee = (
        RegistrationFeeSettings.objects.order_by("pk")
        .values_list("default_consultation_fee", flat=True)
        .first()
    )
    default_discount = (
        RegistrationFeeSettings.objects.order_by("pk")
        .values_list("default_discount", flat=True)
        .first()
    )

    if request.method == "POST":
        form = EmergencyRegistrationForm(request.POST, next_mr=next_mr)
        if form.is_valid():
            data = form.cleaned_data
            reg_fee = default_fee or Decimal("0")
            con_fee = default_consultation_fee or Decimal("0")
            discount = default_discount or Decimal("0")
            bill_total = reg_fee + con_fee
            bill_discount = min(discount, bill_total)
            with transaction.atomic():
                last_locked = Patient.objects.select_for_update().order_by("-id").first()
                current_mr = (
                    int(last_locked.uhid) + 1
                    if (last_locked and last_locked.uhid and last_locked.uhid.isdigit())
                    else 100001
                )
                parts = data["name"].split(maxsplit=1)
                patient = Patient(
                    first_name=parts[0],
                    last_name=parts[1] if len(parts) > 1 else "",
                    gender=data["gender"],
                    date_of_birth=data.get("date_of_birth"),
                    uhid=data["uhid"] or str(current_mr),
                    is_emergency=True,
                    nationality="India",
                    patient_category="GENERAL",
                    billing_type="Individual",
                    payment_type=Patient.PaymentType.PENDING,
                    reg_fee=reg_fee,
                    con_fee=con_fee,
                    discount=discount,
                )
                patient.save()
                # Payment is intentionally left pending and surfaces in the
                # Bill Command Center for collection later.
                bill = PatientBill.objects.create(
                    patient=patient,
                    source=PatientBill.Source.REGISTRATION,
                    description="Emergency registration fee — payment pending",
                    amount=bill_total,
                    discount=bill_discount,
                    status=PatientBill.Status.PENDING,
                    created_by=request.user,
                )
            messages.success(
                request,
                f"Emergency patient {patient.full_name} registered (UHID #{patient.uhid}). "
                f"Pending bill {bill.bill_number} for ₹{bill.amount_due:,.2f} "
                f"(₹{reg_fee} registration + ₹{con_fee} consultation − ₹{bill_discount} discount) created — "
                "collect later in the Bill Command Center. Complete the remaining details from History.",
            )
            return redirect("emergency_registration")
    else:
        form = EmergencyRegistrationForm(next_mr=next_mr)

    recent_emergency = Patient.objects.filter(is_emergency=True).order_by("-created_at")[:6]
    return render(request, "core/emergency_registration.html", {
        "form": form,
        "next_mr": next_mr,
        "today": timezone.localdate(),
        "recent_emergency": recent_emergency,
        "fee_configuration_missing": default_fee is None,
        "default_registration_fee": default_fee,
        "default_consultation_fee": default_consultation_fee,
        "default_discount": default_discount,
        "user": request.user,
    })


@login_required(login_url="login")
def patient_print_card(request, patient_id):
    """Show the print-ready registration card for one patient."""
    from decimal import Decimal
    from django.shortcuts import get_object_or_404

    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig == "doctor" and not request.user.is_superuser:
        from django.contrib import messages
        messages.warning(request, "Access restricted: Patient registration cards are for Registration Desk staff only.")
        return redirect("dashboard")

    patient = get_object_or_404(Patient, pk=patient_id)
    gross_amount = (patient.reg_fee or Decimal("0")) + (patient.con_fee or Decimal("0"))
    discount = patient.discount or Decimal("0")
    net_amount = max(gross_amount - discount, Decimal("0"))
    return render(request, "core/patient_print_card.html", {
        "patient": patient,
        "gross_amount": gross_amount,
        "discount": discount,
        "net_amount": net_amount,
    })


# ── History ───────────────────────────────────────────────────────────────────

def _history_rows(query="", status="", selected_date=None, include_appointments=False):
    """Build the history rows (patient registrations and, optionally, appointments).

    Shared by the History screen and the Registration Desk hub so both show the
    exact same records, filters and row actions.
    """
    from django.db.models import Q
    from django.urls import reverse

    rows = []

    patients = Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(uhid__icontains=query)
            | Q(care_of__icontains=query)
            | Q(phone__icontains=query)
            | Q(aadhar_no__icontains=query)
            | Q(abha_no__icontains=query)
        )
    if selected_date:
        patients = patients.filter(created_at__date=selected_date)
    for patient in patients:
        rows.append({
            "kind": "patient",
            "patient_id": patient.pk,
            "when": patient.created_at,
            "badge": f"Patient · UHID #{patient.uhid or patient.id}",
            "title": patient.full_name,
            "detail": patient.phone or "—",
            "care_of": patient.care_of,
            "status": None,
            "status_lower": "",
            "is_emergency": patient.is_emergency,
            "view_url": reverse("patient_detail", args=[patient.pk]),
            "edit_url": reverse("patient_edit", args=[patient.pk]),
            "delete_url": reverse("patient_delete", args=[patient.pk]),
        })

    if include_appointments:
        appointments = Appointment.objects.select_related("patient", "doctor")
        if query:
            appointments = appointments.filter(
                Q(patient__first_name__icontains=query)
                | Q(patient__second_name__icontains=query)
                | Q(patient__last_name__icontains=query)
                | Q(patient__uhid__icontains=query)
                | Q(patient__care_of__icontains=query)
                | Q(doctor__first_name__icontains=query)
                | Q(doctor__last_name__icontains=query)
                | Q(reason__icontains=query)
            )
        if selected_date:
            appointments = appointments.filter(date=selected_date)
        if status:
            appointments = appointments.filter(status=status)
        from datetime import datetime

        from django.utils import timezone
        for appointment in appointments:
            start = timezone.make_aware(
                datetime.combine(appointment.date, appointment.start_time)
            )
            rows.append({
                "kind": "appointment",
                "when": start,
                "badge": f"Appointment · {appointment.doctor}",
                "title": appointment.patient.full_name,
                "detail": appointment.reason or "—",
                "status": appointment.get_status_display(),
                "status_lower": appointment.status.lower(),
                "view_url": reverse("patient_detail", args=[appointment.patient_id]),
                "edit_url": reverse("appointment_edit", args=[appointment.pk]),
            })

    rows.sort(key=lambda row: (row["when"] is not None, row["when"]), reverse=True)
    return rows[:200]


@login_required(login_url="login")
def history(request):
    """Combined history of patient registrations and booked appointments.

    Supports filtering by record type, calendar date, appointment status and a
    free-text search. Every row links to its edit form so the desk can correct
    information when needed.
    """
    from datetime import datetime

    from django.db.models import Q
    from django.urls import reverse

    is_registration_desk = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    ) == UserProfile.Designation.REGISTRATION_DESK

    item_type = request.GET.get("type", "all")
    if item_type not in ("all", "patients", "appointments"):
        item_type = "all"
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    date_str = request.GET.get("date", "")
    selected_date = None
    if date_str:
        try:
            selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            selected_date = None

    include_patients = item_type in ("all", "patients")
    include_appointments = item_type in ("all", "appointments")

    rows = _history_rows(
        query=query,
        status=status,
        selected_date=selected_date,
        include_appointments=include_appointments,
    ) if include_patients or include_appointments else []

    return render(request, "core/history.html", {
        "rows": rows,
        "row_count": len(rows),
        "item_type": item_type,
        "query": query,
        "status": status,
        "selected_date": selected_date,
        "date_str": date_str,
        "status_choices": Appointment.Status.choices,
        "is_registration_desk": is_registration_desk,
        "user": request.user,
    })


import os
import re as _re
import json as _json
import urllib.request as _urllib_request
import urllib.error as _urllib_error


def _medgemma_mock_draft(vitals, complaints, allergies):
    """Local fallback draft — mirrors the previous client-side mock."""
    v = vitals or {}
    bmi = v.get("bmi") or "—"
    comp = complaints or "Not recorded"
    allergy_text = ", ".join(allergies) if allergies else "—"
    summary = (
        "**Presenting complaint:** " + comp + ".\n"
        "**Vitals:** Temp " + str(v.get("temp", "—")) + "°C, Pulse " + str(v.get("pulse", "—")) + "/min, RR " +
        str(v.get("rr", "—")) + "/min, BP " + str(v.get("bpSys", "—")) + "/" + str(v.get("bpDia", "—")) +
        " mmHg, SpO₂ " + str(v.get("spo2", "—")) + "%, BMI " + str(bmi) + ".\n"
        "**Relevant history:** Type 2 diabetes and hypertension on treatment; allergic to " + allergy_text + ".\n"
        "**Assessment:** Acute febrile illness with hyperglycaemia — review before prescribing."
    )
    differential = (
        "1. **Viral fever** — most likely; supportive care advised.\n"
        "2. **Acute bacterial upper respiratory infection** — consider if symptoms persist.\n"
        "3. **Dengue / tropical infection** — evaluate platelet trend given geography.\n"
        "4. **Uncontrolled diabetes** — RBS " + str(v.get("rbs", "—")) + " mg/dL warrants HbA1c and dose review."
    )
    tests = (
        "- CBC with differential and platelet count\n"
        "- Rapid malarial antigen / dengue NS1 (as clinically indicated)\n"
        "- HbA1c and fasting lipid profile\n"
        "- Urine routine and microscopy\n"
        "- Chest X-ray if respiratory signs develop"
    )
    return {"summary": summary, "differential": differential, "tests": tests}


def _extract_json_text(text):
    """Pull a JSON object out of an LLM reply (handles ```json fences)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = _re.sub(r"^```(?:json)?\s*", "", text)
        text = _re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return _json.loads(text)


def _openai_compatible_draft(vitals, complaints, allergies, patient_name):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    system = (
        "You are a clinical assistant for an OPD triage workstation. "
        "Given patient vitals, complaints and history, return ONLY a valid JSON object "
        "with three string fields: \"summary\", \"differential\", \"tests\". "
        "summary: a concise clinical narrative. differential: a numbered list of likely diagnoses. "
        "tests: a bullet list of suggested investigations. No markdown fences, no extra text."
    )
    user = (
        "Patient: " + (patient_name or "Unknown") + "\n"
        "Vitals: " + _json.dumps(vitals) + "\n"
        "Chief complaints: " + (complaints or "Not recorded") + "\n"
        "Allergies: " + (", ".join(allergies) if allergies else "None recorded")
    )
    body = _json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.3,
    }).encode("utf-8")
    url = base + "/chat/completions"
    req = _urllib_request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with _urllib_request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except _urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError("HTTP %s from %s — %s" % (exc.code, url, detail)) from exc
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json_text(content)
    return {
        "summary": str(parsed.get("summary", "")),
        "differential": str(parsed.get("differential", "")),
        "tests": str(parsed.get("tests", "")),
    }


def _gemini_draft(vitals, complaints, allergies, patient_name):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    system = (
        "You are a clinical assistant for an OPD triage workstation. "
        "Return ONLY a valid JSON object with three string fields: \"summary\", \"differential\", \"tests\". "
        "summary: a concise clinical narrative. differential: a numbered list of likely diagnoses. "
        "tests: a bullet list of suggested investigations. No markdown fences, no extra text."
    )
    user = (
        "Patient: " + (patient_name or "Unknown") + "\n"
        "Vitals: " + _json.dumps(vitals) + "\n"
        "Chief complaints: " + (complaints or "Not recorded") + "\n"
        "Allergies: " + (", ".join(allergies) if allergies else "None recorded")
    )
    url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(model, key)
    body = _json.dumps({
        "contents": [{"parts": [{"text": system + "\n\n" + user}]}],
        "generationConfig": {"temperature": 0.3},
    }).encode("utf-8")
    req = _urllib_request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _urllib_request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except _urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError("HTTP %s from %s — %s" % (exc.code, url.split("?")[0], detail)) from exc
    content = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = _extract_json_text(content)
    return {
        "summary": str(parsed.get("summary", "")),
        "differential": str(parsed.get("differential", "")),
        "tests": str(parsed.get("tests", "")),
    }


@login_required(login_url="login")
@require_POST
def medgemma_draft(request):
    """OPD AI draft endpoint — tries a configured provider, falls back to a local mock."""
    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = {}

    vitals = payload.get("vitals") or {}
    complaints = payload.get("complaints") or ""
    allergies = payload.get("allergies") or []
    patient_name = payload.get("patientName") or ""

    import sys as _sys

    provider = os.environ.get("MEDGEMMA_PROVIDER", "mock").strip().lower()
    _sys.stderr.write("medgemma_draft: provider=%r\n" % provider)
    _sys.stderr.flush()

    result = None
    fallback_reason = None
    if provider not in ("mock", "local", ""):
        try:
            if provider == "gemini":
                result = _gemini_draft(vitals, complaints, allergies, patient_name)
            else:
                result = _openai_compatible_draft(vitals, complaints, allergies, patient_name)
            _sys.stderr.write("medgemma_draft: provider %s succeeded\n" % provider)
            _sys.stderr.flush()
        except Exception as exc:  # noqa: BLE001 — any failure should fall back gracefully
            fallback_reason = str(exc)
            _sys.stderr.write("medgemma_draft: provider %s failed: %s\n" % (provider, fallback_reason))
            _sys.stderr.flush()
            result = None

    if result is None:
        result = _medgemma_mock_draft(vitals, complaints, allergies)
        if fallback_reason:
            result["_fallback"] = fallback_reason

    if not (result.get("summary") and result.get("differential") and result.get("tests")):
        result = _medgemma_mock_draft(vitals, complaints, allergies)

    result["_provider"] = provider if (fallback_reason is None and provider not in ("mock", "local", "")) else "mock"
    return JsonResponse(result)


# ── Initial Assessment (OPD triage) ─────────────────────────────────────────

#: Chronic conditions offered as quick-pick cards during triage.
CHRONIC_CONDITION_OPTIONS = [
    ("diabetes", "Type 2 Diabetes Mellitus"),
    ("hypertension", "Hypertension"),
    ("asthma", "Asthma / COPD"),
    ("cad", "Coronary Artery Disease"),
    ("ckd", "Chronic Kidney Disease"),
    ("thyroid", "Thyroid Disorder"),
]

#: Complaint durations offered in the assessment form.
DURATION_OPTIONS = [
    "Few hours", "1 day", "2 days", "3 days",
    "1 week", "2 weeks", "1 month", "> 1 month",
]

_COMPLAINT_RE = _re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def _parse_complaints(raw):
    """Split the stored complaint blob into structured rows.

    Complaints are stored one per line, optionally suffixed with the duration
    in brackets — e.g. ``Fever with chills (2 days)``. Older records stored a
    JSON list instead, so that shape is understood as well.
    """
    if isinstance(raw, (list, tuple)):
        return [
            {
                "text": str(item.get("text", "")).strip() if isinstance(item, dict) else str(item).strip(),
                "duration": (str(item.get("duration", "")).strip() if isinstance(item, dict) else ""),
            }
            for item in raw
            if str(item.get("text", "") if isinstance(item, dict) else item).strip()
        ]

    text = str(raw or "").strip()
    if text.startswith("["):
        try:
            import json as _json_mod
            parsed = _json_mod.loads(text.replace("'", '"'))
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return _parse_complaints(parsed)

    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _COMPLAINT_RE.match(line)
        if match and match.group(2) in DURATION_OPTIONS:
            rows.append({"text": match.group(1).strip(), "duration": match.group(2)})
        else:
            rows.append({"text": line, "duration": ""})
    return rows


def _normalise_conditions(raw):
    """Normalise stored chronic conditions into ``[{"name", "since"}]``."""
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            since = (item.get("since") or "").strip()
        else:
            name, since = str(item).strip(), ""
        if name:
            out.append({"name": name, "since": since})
    return out


def _normalise_medications(raw):
    """Normalise stored medications into ``[{"name", "dose", "frequency"}]``."""
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            dose = (item.get("dose") or "").strip()
            frequency = (item.get("frequency") or "").strip()
        else:
            name, dose, frequency = str(item).strip(), "", ""
        if name:
            out.append({"name": name, "dose": dose, "frequency": frequency})
    return out


def _normalise_social_history(raw):
    """Normalise stored social history into a list of strings."""
    if isinstance(raw, dict):
        # Legacy shape: {"smoking": True, "alcohol": False}
        return [str(key).title() for key, value in raw.items() if value]
    out = []
    for item in raw or []:
        name = str(item).strip()
        if name:
            out.append(name)
    return out


def _normalise_orders(raw):
    """Normalise the order payload into ``{"laboratory": [...], ...}``."""
    out = {"laboratory": [], "radiology": [], "other": []}
    if isinstance(raw, dict):
        for key in out:
            value = raw.get(key) or []
            if isinstance(value, str):
                value = [value]
            out[key] = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            kind = item.get("type") or "laboratory"
            if name and kind in out:
                out[kind].append(name)
    return out


@login_required(login_url="login")
@require_POST
def save_initial_assessment(request):
    """Persist an OPD initial assessment for a patient (create or update)."""
    from django.utils import timezone
    from patients.models import InitialAssessment, OPDAuditEvent, Patient

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
    )
    allowed = {
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.DOCTOR,
        UserProfile.Designation.ADMIN,
    }
    if designation not in allowed:
        return JsonResponse({"ok": False, "error": "Clinical staff access required."}, status=403)
    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    patient_id = payload.get("patient_id")
    if not patient_id:
        return JsonResponse({"ok": False, "error": "patient_id required"}, status=400)

    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return JsonResponse({"ok": False, "error": "Patient not found"}, status=404)

    # Vitals may arrive flat or nested under a "vitals" key.
    vitals = payload.get("vitals") if isinstance(payload.get("vitals"), dict) else {}

    def _value(key, default=None):
        if key in payload:
            return payload.get(key)
        if key in vitals:
            return vitals.get(key)
        return default

    def _num(v, cast=int):
        if v is None or v == "":
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    numeric_fields = {
        "temperature": (float, 25, 45),
        "pulse": (int, 1, 300),
        "respiratory_rate": (int, 1, 100),
        "bp_systolic": (int, 1, 350),
        "bp_diastolic": (int, 1, 250),
        "spo2": (int, 1, 100),
        "height": (float, 20, 250),
        "weight": (float, 0.2, 500),
        "bmi": (float, 1, 150),
        "rbs": (int, 1, 2000),
        "pain_score": (int, 0, 10),
    }
    values = {}
    for field, (cast, lower, upper) in numeric_fields.items():
        raw_value = _value(field)
        values[field] = _num(raw_value, cast)
        if raw_value not in (None, "") and values[field] is None:
            return JsonResponse({"ok": False, "error": f"{field.replace('_', ' ').title()} must be numeric."}, status=400)
        if values[field] is not None and not lower <= values[field] <= upper:
            return JsonResponse({"ok": False, "error": f"{field.replace('_', ' ').title()} is outside the accepted entry range ({lower}-{upper})."}, status=400)

    action = payload.get("action", "draft")
    if action not in ("draft", "complete"):
        return JsonResponse({"ok": False, "error": "Choose draft or complete."}, status=400)
    complaint_payload = payload.get("complaints") or []
    has_complaint = (
        bool(complaint_payload.strip()) if isinstance(complaint_payload, str)
        else any(
            str(item.get("text", "") if isinstance(item, dict) else item).strip()
            for item in complaint_payload
        )
    )
    if action == "complete" and not has_complaint:
        return JsonResponse({"ok": False, "error": "A chief complaint is required to complete the assessment."}, status=400)
    assessment, _ = InitialAssessment.objects.get_or_create(
        patient=patient, defaults={"status": InitialAssessment.Status.DRAFT}
    )
    for field, value in values.items():
        setattr(assessment, field, value)

    complaints = payload.get("complaints")
    if isinstance(complaints, str):
        assessment.complaints = complaints.strip()
    else:
        lines = []
        for item in complaints or []:
            text = (item.get("text", "") if isinstance(item, dict) else str(item)).strip()
            duration = (item.get("duration", "") if isinstance(item, dict) else "").strip()
            if not text:
                continue
            lines.append(f"{text} ({duration})" if duration else text)
        assessment.complaints = "\n".join(lines)

    assessment.hpi = payload.get("hpi", "") or ""
    assessment.summary_note = payload.get("summary_note", "") or ""
    assessment.surgeries = payload.get("surgeries", "") or ""

    assessment.chronic_conditions = _normalise_conditions(payload.get("chronic_conditions"))
    assessment.allergies = [str(a).strip() for a in (payload.get("allergies") or []) if str(a).strip()]
    assessment.medications = _normalise_medications(payload.get("medications"))
    assessment.social_history = _normalise_social_history(payload.get("social_history"))
    assessment.examination = payload.get("examination") or {}
    assessment.orders = _normalise_orders(payload.get("orders"))
    assessment.status = (
        InitialAssessment.Status.COMPLETED if action == "complete" else InitialAssessment.Status.DRAFT
    )
    assessment.completed_by = request.user if action == "complete" else None
    assessment.completed_at = timezone.now() if action == "complete" else None

    assessment.save()

    OPDAuditEvent.objects.create(
        patient=patient,
        actor=request.user,
        action="assessment_completed" if action == "complete" else "assessment_draft_saved",
        summary="Initial assessment completed and routed to doctor queue" if action == "complete" else "Initial assessment draft saved",
        details={"status": assessment.status, "urgent": bool(payload.get("urgent"))},
    )

    if payload.get("urgent") and not patient.is_emergency:
        patient.is_emergency = True
        patient.save(update_fields=["is_emergency", "updated_at"])

    if action == "complete":
        _return_ticket_to_opd_desk(patient, request.user)

    return JsonResponse({"ok": True, "id": assessment.pk})


def _return_ticket_to_opd_desk(patient, user):
    """Close any open nurse ticket and mark the visit back at the OPD Desk.

    Called whenever an initial assessment is completed, regardless of which
    desk recorded it, so the OPD Desk always regains ownership of the visit
    for doctor allocation.
    """
    from patients.models import NurseQueueTicket, OPDAuditEvent

    open_ticket = _nurse_ticket_for_patient(patient)
    if open_ticket is None:
        return None
    open_ticket.status = NurseQueueTicket.Status.COMPLETED
    open_ticket.completed_by = user
    open_ticket.completed_at = timezone.now()
    open_ticket.returned_at = timezone.now()
    open_ticket.save(update_fields=["status", "completed_by", "completed_at", "returned_at"])
    OPDAuditEvent.objects.create(
        patient=patient,
        actor=user,
        action="nurse_ticket_returned",
        summary=f"{open_ticket.ticket_number} returned to the OPD Desk for doctor allocation",
        details={"ticket_id": open_ticket.pk},
    )
    return open_ticket


@login_required(login_url="login")
@require_POST
def initial_assessment_requeue(request):
    """Send a visit from the OPD Desk back to the nurse queue for re-assessment.

    Used by the OPD assistant when an assessment needs a re-check (e.g. vitals
    incomplete or a repeat BP). Body: ``{"patient_id": <id>, "reason": …}``.
    """
    from patients.models import NurseQueueTicket, OPDAuditEvent

    designation = _designation_of(request)
    if not request.user.is_superuser and designation not in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.ADMIN,
    ):
        return JsonResponse({"ok": False, "error": "OPD Desk access required."}, status=403)
    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    patient = Patient.objects.filter(pk=payload.get("patient_id")).first()
    if patient is None:
        return JsonResponse({"ok": False, "error": "Patient not found"}, status=404)

    ticket = _nurse_ticket_for_patient(patient)
    if ticket is None:
        active_appointment = (
            Appointment.objects.filter(
                patient=patient,
                date__gte=timezone.localdate(),
            )
            .filter(status=Appointment.Status.SCHEDULED)
            .order_by("date", "start_time")
            .first()
        )
        ticket = NurseQueueTicket.objects.create(
            patient=patient,
            appointment=active_appointment,
            priority=_default_priority_for(patient),
            source=NurseQueueTicket.Source.OPD_DESK,
            sent_by=request.user,
        )
    ticket.status = NurseQueueTicket.Status.WAITING
    ticket.priority = payload.get("priority") if payload.get("priority") in NurseQueueTicket.Priority.values else ticket.priority
    ticket.visit_note = str(payload.get("reason") or "Re-assessment requested by OPD Desk")[:255]
    ticket.returned_at = None
    ticket.completed_by = None
    ticket.completed_at = None
    ticket.claimed_by = None
    ticket.claimed_at = None
    ticket.save()
    OPDAuditEvent.objects.create(
        patient=patient,
        actor=request.user,
        action="nurse_sent",
        summary=f"Re-assessment requested — {ticket.ticket_number} reopened in the triage queue",
        details={"ticket_id": ticket.pk, "reason": ticket.visit_note},
    )
    return JsonResponse({"ok": True, "ticket_id": ticket.pk, "ticket_number": ticket.ticket_number})


@login_required(login_url="login")
def initial_assessment(request):
    """OPD triage workstation — pick a patient, then record vitals, complaints
    and history before consultation.

    Renders inside the standard app shell (sidebar on the left). The page is a
    two column workstation: the assessment form on the left and a persistent
    patient search / list on the right. Picking a patient from that list opens
    their assessment form; ``?patient=<id>`` selects one directly.
    """
    from django.db.models import Exists, OuterRef, Q
    from django.utils import timezone

    from patients.models import InitialAssessment

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if not request.user.is_superuser and designation not in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.DOCTOR,
        UserProfile.Designation.ADMIN,
    ):
        # Keep the page's guard in sync with save_initial_assessment's allow
        # set — service staff / pharmacy / registration must not open a form
        # whose save endpoint would only 403 them.
        messages.warning(
            request,
            "Access restricted: the Initial Assessment workstation is for clinical staff.",
        )
        if designation == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        if designation == UserProfile.Designation.SERVICE_STAFF:
            return redirect("service_staff_dashboard")
        if designation == UserProfile.Designation.PHARMACY:
            return redirect("pharmacy_dashboard")
        return redirect("dashboard")

    # ── Patient search / picker list ───────────────────────────────────────
    query = request.GET.get("q", "").strip()
    patients = Patient.objects.annotate(
        has_assessment=Exists(InitialAssessment.objects.filter(patient=OuterRef("pk")))
    )
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(uhid__icontains=query)
            | Q(phone__icontains=query)
        )
    patients = patients.order_by("-created_at")[:30]

    # Selected patient (if any)
    patient = None
    patient_id = request.GET.get("patient", "").strip()
    if patient_id.isdigit():
        patient = Patient.objects.filter(pk=int(patient_id)).first()

    # Blank assessment payload — used when no patient is selected.
    assessment_data = {
        "temperature": "", "pulse": "", "respiratory_rate": "",
        "bp_systolic": "", "bp_diastolic": "", "spo2": "",
        "height": "", "weight": "", "bmi": "", "rbs": "", "pain_score": "",
        "complaints": [], "hpi": "", "summary_note": "", "surgeries": "",
        "chronic_conditions": [], "conditions": [], "custom_conditions": [],
        "allergies": [], "medications": [], "social_history": [],
        "examination": {"general": "", "cvs": "", "respiratory": "", "abdomen": "", "cns": ""},
        "orders": {"laboratory": [], "radiology": [], "other": []},
        "saved": False,
    }

    patient_ctx = None
    if patient:
        now = timezone.now()
        department = "General Medicine"
        if patient.assigned_doctor:
            profile = getattr(patient.assigned_doctor, "profile", None)
            if profile and profile.specialty:
                department = profile.get_specialty_display()

        allergies = []
        if patient.allergies:
            allergies = [
                part.strip()
                for part in patient.allergies.replace(";", ",").split(",")
                if part.strip()
            ]

        initials = "PT"
        parts = [p for p in (patient.first_name, patient.last_name) if p]
        initials = "".join(p[0] for p in parts[:2]).upper() or "PT"

        patient_ctx = {
            "id": patient.pk,
            "name": patient.full_name or "—",
            "initials": initials,
            "uhid": patient.uhid,
            "visit_token": f"{now:%y%m%d}-{patient.pk:04d}",
            "age": patient.age if patient.age is not None else "—",
            "gender": patient.get_gender_display() if patient.gender else "—",
            "phone": patient.phone or "—",
            "address": patient.address or "—",
            "visit_timestamp": timezone.localtime(patient.created_at).strftime("%d %b %Y, %I:%M %p"),
            "department": department,
            "visit_type": "Emergency" if patient.is_emergency else "Walk-in · New Visit",
            "allergies": allergies,
            "chronic_illnesses": [],
            "active_medications": [],
            "is_emergency": bool(patient.is_emergency),
        }

        # Load any previously saved assessment for pre-filling the form
        try:
            a = patient.initial_assessment
        except InitialAssessment.DoesNotExist:
            a = None

        if a is not None:
            def _txt(value):
                return "" if value is None else str(value)

            assessment_data.update({
                "temperature": _txt(a.temperature),
                "pulse": _txt(a.pulse),
                "respiratory_rate": _txt(a.respiratory_rate),
                "bp_systolic": _txt(a.bp_systolic),
                "bp_diastolic": _txt(a.bp_diastolic),
                "spo2": _txt(a.spo2),
                "height": _txt(a.height),
                "weight": _txt(a.weight),
                "bmi": _txt(a.bmi),
                "rbs": _txt(a.rbs),
                "pain_score": _txt(a.pain_score),
                "complaints": _parse_complaints(a.complaints),
                "hpi": a.hpi or "",
                "summary_note": a.summary_note or "",
                "surgeries": a.surgeries or "",
                "allergies": [str(x) for x in (a.allergies or [])],
                "medications": _normalise_medications(a.medications),
                "social_history": _normalise_social_history(a.social_history),
                "examination": a.examination or {},
                "orders": _normalise_orders(a.orders),
                "chronic_conditions": _normalise_conditions(a.chronic_conditions),
                "saved": a.status == InitialAssessment.Status.COMPLETED,
            })

        # Split the conditions into the quick-pick cards and any free-text
        # "others" the nurse typed in.
        remaining = list(assessment_data["chronic_conditions"])
        conditions = []
        for key, label in CHRONIC_CONDITION_OPTIONS:
            match = next(
                (c for c in remaining if c["name"].lower() == label.lower()),
                None,
            )
            if match:
                remaining.remove(match)
            conditions.append({
                "key": key,
                "label": label,
                "checked": bool(match),
                "since": match["since"] if match else "",
            })
        assessment_data["conditions"] = conditions
        assessment_data["custom_conditions"] = remaining

        # Allergies recorded in the assessment supplement the patient's record.
        merged = list(patient_ctx["allergies"])
        for item in assessment_data["allergies"]:
            if item.lower() not in [m.lower() for m in merged]:
                merged.append(item)
        assessment_data["allergies"] = merged
        patient_ctx["allergies"] = merged

    return render(request, "core/initial_assessment.html", {
        "patient_ctx": patient_ctx,
        "assessment": assessment_data,
        "has_patient": patient is not None,
        "selected_patient": patient,
        "patients": patients,
        "query": query,
        "condition_options": CHRONIC_CONDITION_OPTIONS,
        "duration_options": DURATION_OPTIONS,
        "user": request.user,
    })


@login_required(login_url="login")
def initial_assessment_patients(request):
    """Live patient search used by the Initial Assessment patient picker.

    Returns a small JSON payload (name, UHID, demographics, assessment flag and
    a deep link) so the picker popup can search without a page reload.
    """
    from django.db.models import Exists, OuterRef, Q
    from django.urls import reverse

    from patients.models import InitialAssessment

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
    )
    if designation not in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.DOCTOR,
        UserProfile.Designation.ADMIN,
    ):
        return JsonResponse({"ok": False, "error": "Clinical staff access required."}, status=403)

    query = request.GET.get("q", "").strip()
    patients = Patient.objects.annotate(
        has_assessment=Exists(InitialAssessment.objects.filter(patient=OuterRef("pk")))
    )
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(uhid__icontains=query)
            | Q(phone__icontains=query)
        )
    patients = patients.order_by("-created_at")[:20]

    base_url = reverse("initial_assessment")
    results = []
    for p in patients:
        initials = "".join(part[0] for part in (p.first_name, p.last_name) if part)[:2].upper() or "PT"
        meta = "%s · %s yrs · %s" % (
            p.uhid,
            p.age if p.age is not None else "—",
            p.get_gender_display() if p.gender else "—",
        )
        if p.phone:
            meta += " · %s" % p.phone
        results.append({
            "id": p.pk,
            "name": p.full_name or "—",
            "initials": initials,
            "uhid": p.uhid,
            "meta": meta,
            "emergency": bool(p.is_emergency),
            "has_assessment": bool(p.has_assessment),
            "url": "%s?patient=%s" % (base_url, p.pk),
            "search": " ".join([p.full_name.lower(), p.uhid.lower(), p.phone.lower()]),
        })

    return JsonResponse({"ok": True, "query": query, "results": results})


# ── Doctor portal: central patient queue & service hub ─────────────────────────

DOCTOR_QUEUE_STATUS = {
    "Doctor Allocation": "Waiting to start",
    "With Doctor": "With you",
    "Investigations": "Investigations",
    "Procedures": "Procedures",
    "Pharmacy": "Pharmacy",
    "Review Required": "Review required",
    "Awaiting Payment": "Payment pending",
    "Assessment": "Pre-assessment",
    "Waiting": "Pre-assessment",
    "Completed": "Completed",
    "Cancelled": "Cancelled",
}

DOCTOR_QUEUE_STATUS_RANK = {
    "Doctor Allocation": 0, "With Doctor": 1, "Investigations": 2,
    "Procedures": 3, "Pharmacy": 4, "Review Required": 5,
    "Awaiting Payment": 6, "Assessment": 7, "Waiting": 7,
    "Completed": 8, "Cancelled": 9,
}


@login_required(login_url="login")
def doctor_patient_queue(request):
    """The doctor's central queue over the same pool the OPD desk manages.

    Two worklists:
      * My patients — visits assigned to this doctor (today's pool, or every
        assigned patient with the ``?all=1`` toggle).
      * Unassigned pool — assessed visits no doctor claimed yet, offered only
        when they pass the assignment gate, each pickable in one click.
    """
    from django.db.models import Exists, OuterRef, Q, Subquery
    from django.utils import timezone as dj_tz

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if designation not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN) and not request.user.is_superuser:
        messages.warning(request, "Access restricted: the patient queue is for doctors.")
        if designation == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        if designation == UserProfile.Designation.OPD_ASSISTANT:
            return redirect("opd_assistant_dashboard")
        return redirect("dashboard")

    today = dj_tz.localdate()
    is_doctor = designation == UserProfile.Designation.DOCTOR and not request.user.is_superuser
    show_all = request.GET.get("all") == "1"
    query = request.GET.get("q", "").strip()
    # Board = ED-style tracking census of today's visits (default); List =
    # dense scannable rows. The "All my patients" register is a list concept.
    view = request.GET.get("view", "board")
    if view not in ("board", "list"):
        view = "board"
    if show_all:
        view = "list"
    sort = request.GET.get("sort", "urgency")

    pool_patients = list(
        Patient.objects.filter(
            Q(created_at__date=today)
            | Q(appointments__date=today)
            | Q(appointments__created_at__date=today)
            | Q(nurse_tickets__sent_at__date=today) | Q(nurse_tickets__completed_at__date=today)
        ).distinct()
        .select_related("assigned_doctor", "assigned_doctor__profile")
        .prefetch_related(
            "consultations__orders",
            "consultations__prescriptions",
            "consultations__bills",
            "consultations__pharmacy_issues",
            "bills",
            "appointments__bills",
        )
        .order_by("-is_emergency", "created_at")[:500]
    )
    rows = _opd_patient_flow(pool_patients)
    for row in rows:
        row["doctor_status"] = DOCTOR_QUEUE_STATUS.get(row["status"], row["status"])

    if query:
        needle = query.casefold()
        rows = [
            row for row in rows
            if needle in " ".join((
                row["patient"].full_name, row["patient"].uhid or "",
                row["patient"].phone or "",
            )).casefold()
        ]

    if is_doctor:
        mine_rows = [row for row in rows if row["doctor_id"] == request.user.pk]
    else:
        # Administrators observe every assigned visit instead of "mine".
        mine_rows = [row for row in rows if row["doctor_id"] is not None]
    # Unassigned = assessed and passing the assignment gate, with no doctor
    # claimed yet (needs_doctor alone also matches assigned-but-not-started).
    pool_rows = [
        row for row in rows
        if row["needs_doctor"] and row["doctor_id"] is None
    ]
    mine_rows.sort(key=lambda r: (
        not r["patient"].is_emergency,
        DOCTOR_QUEUE_STATUS_RANK.get(r["status"], 5),
        r["patient"].created_at,
    ))
    pool_rows.sort(key=lambda r: (not r["patient"].is_emergency, r["patient"].created_at))

    # Wait times for the escalation pills (green < 15m, amber < 30m, red 30m+).
    now = dj_tz.now()
    for row in mine_rows + pool_rows:
        row["wait_minutes"] = max(
            0, int((now - row["patient"].created_at).total_seconds() // 60)
        )
    if view == "list" and not show_all:
        if sort == "arrival":
            mine_rows.sort(key=lambda r: r["patient"].created_at, reverse=True)
        elif sort == "name":
            mine_rows.sort(key=lambda r: r["patient"].full_name.casefold())

    # Board census: six tracking-board columns over today's visits.
    board_columns = [
        {
            "key": "intake",
            "title": "Unassigned — waiting for a doctor",
            "tone": "red",
            "rows": pool_rows,
        },
        {
            "key": "waiting",
            "title": "Waiting to start",
            "tone": "amber",
            "rows": [
                r for r in mine_rows
                if r["status"] in ("Doctor Allocation", "Assessment", "Waiting")
            ],
        },
        {
            "key": "withyou",
            "title": "With you",
            "tone": "blue",
            "rows": [
                r for r in mine_rows
                if r["status"] in ("With Doctor", "Review Required")
            ],
        },
        {
            "key": "investigations",
            "title": "Investigations",
            "tone": "sky",
            "rows": [r for r in mine_rows if r["status"] == "Investigations"],
        },
        {
            "key": "services",
            "title": "Services · Pharmacy",
            "tone": "cyan",
            "rows": [
                r for r in mine_rows
                if r["status"] in ("Procedures", "Pharmacy", "Awaiting Payment")
            ],
        },
        {
            "key": "completed",
            "title": "Completed",
            "tone": "green",
            "rows": [r for r in mine_rows if r["status"] in ("Completed", "Cancelled")],
        },
    ]
    census = [
        {"label": col["title"], "tone": col["tone"], "count": len(col["rows"])}
        for col in board_columns
    ]

    # "All my patients" toggle — every patient assigned to this doctor,
    # regardless of visit day.
    all_mine = []
    my_total = len(mine_rows)
    if is_doctor:
        my_total = Patient.objects.filter(assigned_doctor=request.user).count()
        if show_all:
            from patients.models import ConsultationSession
            all_mine = list(
                Patient.objects.filter(assigned_doctor=request.user)
                .select_related("assigned_doctor", "assigned_doctor__profile")
                .annotate(
                    open_consultation=Exists(
                        ConsultationSession.objects.filter(
                            patient=OuterRef("pk"),
                            status__in=[
                                ConsultationSession.Status.DRAFT,
                                ConsultationSession.Status.IN_PROGRESS,
                            ],
                        )
                    ),
                    last_visit=Subquery(
                        ConsultationSession.objects.filter(patient=OuterRef("pk"))
                        .order_by("-created_at")
                        .values("created_at")[:1]
                    ),
                )
                .order_by("-is_emergency", "-created_at")[:200]
            )
            if query:
                all_mine = [
                    p for p in all_mine
                    if query.casefold() in " ".join((
                        p.full_name, p.uhid or "", p.phone or "",
                    )).casefold()
                ]

    return render(request, "core/doctor_patient_queue.html", {
        "today": today,
        "is_doctor": is_doctor,
        "show_all": show_all,
        "view": view,
        "sort": sort,
        "query": query,
        "mine_rows": all_mine if show_all else mine_rows,
        "pool_rows": pool_rows,
        "board_columns": board_columns,
        "census": census,
        "my_total": my_total,
        "counts": {
            "mine": len(all_mine) if show_all else len(mine_rows),
            "waiting": sum(1 for r in mine_rows if r["status"] == "Doctor Allocation"),
            "unassigned": len(pool_rows),
        },
    })


@login_required(login_url="login")
def doctor_service_hub(request):
    """Doctor landing for the department service queues (pharmacy excluded —
    its queue console is for pharmacy staff)."""
    from django.utils import timezone as dj_tz

    from patients.models import ServiceDepartment

    designation = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if designation not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN) and not request.user.is_superuser:
        messages.warning(request, "Access restricted: the service queues are for clinical staff.")
        if designation == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        if designation == UserProfile.Designation.OPD_ASSISTANT:
            return redirect("opd_assistant_dashboard")
        return redirect("dashboard")

    return render(request, "core/doctor_service_hub.html", {
        "page_title": "Service Queues",
        "today": dj_tz.localdate(),
        "departments": ServiceDepartment.objects.order_by("name"),
    })


# ── Doctor portal placeholders ────────────────────────────────────────────────

DOCTOR_PAGES = {
    "initial_assessment": "Initial Assessment",
    "patient_queue": "Patient Queue",
    "doctor_consultation": "Doctor Consultation",
    "service_queues": "Service Queues",
    "procedures": "Procedures",
    "pharmacy": "Pharmacy",
    "doctor_appointments": "Appointments",
    "reports": "Reports",
    "master_data": "Master Data",
    "audit_trail": "Audit Trail",
    "doctor_settings": "Settings",
}


@login_required(login_url="login")
def doctor_placeholder(request, page):
    """Empty placeholder page for a doctor-portal section.

    Content for each section will be filled in by later prompts. For now this
    renders a clean "coming soon" shell so the navigation and routing work.
    """
    if page not in DOCTOR_PAGES:
        from django.http import Http404
        raise Http404

    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN) and not request.user.is_superuser:
        messages.warning(request, "Access restricted: Doctor portal only.")
        if user_desig == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        if user_desig == UserProfile.Designation.OPD_ASSISTANT:
            return redirect("opd_assistant_dashboard")
        return redirect("dashboard")

    return render(request, "core/doctor_placeholder.html", {
        "title": DOCTOR_PAGES[page],
        "page": page,
        "user": request.user,
    })


# ── Doctor Consultation (OPD Desk) ───────────────────────────────────────────

STANDARD_FORMULARY = [
    {"name": "Paracetamol 500 mg Tablet", "generic": "Paracetamol", "category": "Analgesics", "dose": "500 mg", "route": "Oral", "default_freq": "TDS", "default_dur": "5 days", "instructions": "For fever and body ache"},
    {"name": "Paracetamol 650 mg Tablet", "generic": "Paracetamol", "category": "Analgesics", "dose": "650 mg", "route": "Oral", "default_freq": "TDS", "default_dur": "3 days", "instructions": "After food"},
    {"name": "Ibuprofen 400 mg Tablet", "generic": "Ibuprofen", "category": "Analgesics", "dose": "400 mg", "route": "Oral", "default_freq": "BD", "default_dur": "3 days", "instructions": "After food / With milk"},
    {"name": "Tramadol 50 mg Capsule", "generic": "Tramadol", "category": "Analgesics", "dose": "50 mg", "route": "Oral", "default_freq": "SOS", "default_dur": "3 days", "instructions": "For severe pain only"},
    {"name": "Amoxicillin-Clavulanate 625 mg Tablet", "generic": "Amoxicillin-Clavulanate", "category": "Antibiotics", "dose": "625 mg", "route": "Oral", "default_freq": "BD", "default_dur": "5 days", "instructions": "Complete full course after food"},
    {"name": "Azithromycin 500 mg Tablet", "generic": "Azithromycin", "category": "Antibiotics", "dose": "500 mg", "route": "Oral", "default_freq": "OD", "default_dur": "3 days", "instructions": "1 hour before food or 2 hours after"},
    {"name": "Cefixime 200 mg Tablet", "generic": "Cefixime", "category": "Antibiotics", "dose": "200 mg", "route": "Oral", "default_freq": "BD", "default_dur": "5 days", "instructions": "After food"},
    {"name": "Ciprofloxacin 500 mg Tablet", "generic": "Ciprofloxacin", "category": "Antibiotics", "dose": "500 mg", "route": "Oral", "default_freq": "BD", "default_dur": "5 days", "instructions": "Drink plenty of water"},
    {"name": "Amlodipine 5 mg Tablet", "generic": "Amlodipine", "category": "Antihypertensives", "dose": "5 mg", "route": "Oral", "default_freq": "OD", "default_dur": "30 days", "instructions": "Morning after breakfast (Continue)"},
    {"name": "Telmisartan 40 mg Tablet", "generic": "Telmisartan", "category": "Antihypertensives", "dose": "40 mg", "route": "Oral", "default_freq": "OD", "default_dur": "30 days", "instructions": "Same time daily (Continue)"},
    {"name": "Metoprolol Succinate 25 mg Tablet", "generic": "Metoprolol", "category": "Antihypertensives", "dose": "25 mg", "route": "Oral", "default_freq": "OD", "default_dur": "30 days", "instructions": "After breakfast"},
    {"name": "Metformin 500 mg Tablet", "generic": "Metformin", "category": "Antidiabetics", "dose": "500 mg", "route": "Oral", "default_freq": "BD", "default_dur": "30 days", "instructions": "With or immediately after main meals"},
    {"name": "Glimepiride 1 mg Tablet", "generic": "Glimepiride", "category": "Antidiabetics", "dose": "1 mg", "route": "Oral", "default_freq": "OD", "default_dur": "30 days", "instructions": "Before breakfast"},
    {"name": "Pantoprazole 40 mg Tablet", "generic": "Pantoprazole", "category": "GI Drugs", "dose": "40 mg", "route": "Oral", "default_freq": "OD", "default_dur": "14 days", "instructions": "Empty stomach 30 mins before breakfast"},
    {"name": "Omeprazole 20 mg Capsule", "generic": "Omeprazole", "category": "GI Drugs", "dose": "20 mg", "route": "Oral", "default_freq": "OD", "default_dur": "14 days", "instructions": "Before morning food"},
    {"name": "Ondansetron 4 mg Tablet", "generic": "Ondansetron", "category": "GI Drugs", "dose": "4 mg", "route": "Oral", "default_freq": "SOS", "default_dur": "3 days", "instructions": "For nausea / vomiting"},
    {"name": "Cetirizine 10 mg Tablet", "generic": "Cetirizine", "category": "Respiratory", "dose": "10 mg", "route": "Oral", "default_freq": "HS", "default_dur": "5 days", "instructions": "At night, may cause mild drowsiness"},
    {"name": "Montelukast 10 mg Tablet", "generic": "Montelukast", "category": "Respiratory", "dose": "10 mg", "route": "Oral", "default_freq": "HS", "default_dur": "10 days", "instructions": "At bedtime"},
    {"name": "Salbutamol Inhaler (100 mcg)", "generic": "Salbutamol", "category": "Respiratory", "dose": "2 puffs", "route": "Inhalation", "default_freq": "SOS", "default_dur": "30 days", "instructions": "Inhale as needed for wheezing/breathlessness"},
    {"name": "Vitamin B-Complex + Zinc Capsule", "generic": "Multivitamin", "category": "Others", "dose": "1 cap", "route": "Oral", "default_freq": "OD", "default_dur": "15 days", "instructions": "After meals"},
]

COMMON_ICD10 = [
    {"code": "J06.9", "desc": "Acute Upper Respiratory Infection, unspecified"},
    {"code": "E11.9", "desc": "Type 2 Diabetes Mellitus without complications"},
    {"code": "I10", "desc": "Essential (primary) Hypertension"},
    {"code": "K29.7", "desc": "Gastritis, unspecified"},
    {"code": "J45.9", "desc": "Asthma, unspecified"},
    {"code": "M54.5", "desc": "Low Back Pain"},
    {"code": "R50.9", "desc": "Fever, unspecified"},
    {"code": "A09", "desc": "Infectious Gastroenteritis and Colitis, unspecified"},
    {"code": "J20.9", "desc": "Acute Bronchitis, unspecified"},
    {"code": "N39.0", "desc": "Urinary Tract Infection, site not specified"},
    {"code": "K21.9", "desc": "Gastro-esophageal Reflux Disease without esophagitis"},
    {"code": "R10.4", "desc": "Abdominal Pain, other and unspecified"},
]

#: One-tap investigation / procedure banks shown to the doctor.
DOCTOR_ORDER_BANKS = [
    ("laboratory", "Laboratory", [
        "Complete Blood Count (CBC)", "C-Reactive Protein (CRP)", "Erythrocyte Sedimentation Rate (ESR)",
        "Liver Function Test (LFT)", "Renal Function Test (RFT)", "Blood Sugar (RBS / FBS)",
        "HbA1c", "Serum Electrolytes", "Urine Routine & Microscopy", "Urine Culture",
        "Dengue NS1 Antigen", "Thyroid Profile (T3 / T4 / TSH)",
    ]),
    ("radiology", "Radiology & Imaging", [
        "Chest X-Ray (PA View)", "USG Whole Abdomen", "X-Ray Limb", "CT Head",
        "ECG (12 Lead)", "2D Echocardiography",
    ]),
    ("other", "Procedures & Services", [
        "Nebulisation", "IV Fluids", "Wound Dressing", "Injection (as advised)",
        "Physiotherapy Referral", "Counselling Referral",
    ]),
]

#: Patient education checkboxes available on the advice tab.
PATIENT_EDUCATION_PRESETS = [
    "Take medicines exactly as prescribed",
    "Complete the full antibiotic course",
    "Maintain hydration and oral intake",
    "Low salt / diabetic diet advice",
    "Rest and avoid strenuous activity",
    "Return if symptoms worsen or red flags appear",
    "Follow-up on the scheduled date",
]


@login_required(login_url="login")
def doctor_consultation(request):
    """Doctor Consultation (Doctor's Desk) workstation.
    
    Provides complete 7-tab clinical ergonomics:
    1. Assessment
    2. Examination
    3. Diagnosis & Plan
    4. Orders
    5. Reports & Results
    6. Prescription
    7. Advice & Follow-up
    """
    import json
    from django.db.models import (
        BooleanField, Case, Exists, OuterRef, Q, Subquery, Value, When,
    )
    from django.utils import timezone
    from patients.models import (
        Patient, InitialAssessment, ConsultationSession,
        PrescriptionItem, ClinicalOrder, DiagnosticReport, NurseQueueTicket,
        ServiceDefinition,
    )

    user_desig = normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN):
        messages.warning(request, "Access restricted: Doctor portal only.")
        if user_desig == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        if user_desig == UserProfile.Designation.OPD_ASSISTANT:
            return redirect("opd_assistant_dashboard")
        return redirect("dashboard")

    def _wait_label(minutes):
        if minutes <= 0:
            return "0 min"
        if minutes >= 1440:
            return f"{minutes // 1440} d"
        if minutes >= 60:
            hours = minutes // 60
            remaining = minutes % 60
            return f"{hours} h {remaining} min" if remaining else f"{hours} h"
        return f"{minutes} min"

    # Patient search query. Annotate workflow state for the picker rail.
    query = request.GET.get("q", "").strip()
    latest_ticket = NurseQueueTicket.objects.filter(
        patient=OuterRef("pk"),
        status=NurseQueueTicket.Status.COMPLETED,
    ).order_by("-completed_at", "-sent_at")
    patients = Patient.objects.annotate(
        has_assessment=Exists(
            InitialAssessment.objects.filter(
                patient=OuterRef("pk"),
                status=InitialAssessment.Status.COMPLETED,
            )
        ),
        has_active_consultation=Exists(
            ConsultationSession.objects.filter(
                patient=OuterRef("pk"),
                status__in=[
                    ConsultationSession.Status.DRAFT,
                    ConsultationSession.Status.IN_PROGRESS,
                ],
            )
        ),
        triage_priority=Subquery(latest_ticket.values("priority")[:1]),
        triage_completed_at=Subquery(latest_ticket.values("completed_at")[:1]),
        # Mirrors assign_doctor's assessment gate so the doctor's unassigned
        # pool only lists visits a doctor can genuinely pick: an assessment
        # completed today, or a completed triage ticket for the active visit.
        assessment_today=Exists(
            InitialAssessment.objects.filter(
                patient=OuterRef("pk"),
                status=InitialAssessment.Status.COMPLETED,
                completed_at__date=timezone.localdate(),
            )
        ),
        active_visit_ticket_done=Exists(
            NurseQueueTicket.objects.filter(
                patient=OuterRef("pk"),
                status=NurseQueueTicket.Status.COMPLETED,
                appointment__status=Appointment.Status.SCHEDULED,
                appointment__date__gte=timezone.localdate(),
            )
        ),
    )
    if user_desig == UserProfile.Designation.DOCTOR:
        patients = patients.annotate(
            is_assigned=Case(
                When(assigned_doctor=request.user, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
    else:
        patients = patients.annotate(
            is_assigned=Value(False, output_field=BooleanField())
        )
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(uhid__icontains=query)
            | Q(phone__icontains=query)
        )
    patients = patients.order_by("-is_assigned", "-has_assessment", "-created_at")[:40]
    patients = list(patients)
    queue_now = timezone.now()
    for listed_patient in patients:
        reference_time = listed_patient.triage_completed_at or listed_patient.created_at
        listed_patient.wait_minutes = max(
            0, int((queue_now - reference_time).total_seconds() // 60)
        )
        listed_patient.wait_label = _wait_label(listed_patient.wait_minutes)
        listed_patient.is_ready = bool(
            listed_patient.has_assessment and not listed_patient.has_active_consultation
        )
    if user_desig == UserProfile.Designation.DOCTOR:
        ready_patients = [
            p for p in patients if p.is_ready and p.is_assigned
        ][:12]
        # The central queue's unassigned pool: assessed visits no doctor has
        # claimed yet. Doctors can pick these themselves straight from here —
        # only visits that pass the assignment assessment gate are listed.
        unassigned_ready_patients = [
            p for p in patients
            if p.is_ready and p.assigned_doctor_id is None
            and (p.assessment_today or p.active_visit_ticket_done)
        ][:12]
    else:
        ready_patients = [p for p in patients if p.is_ready][:12]
        unassigned_ready_patients = []

    # Selected patient
    patient_id = request.GET.get("patient", "").strip()
    patient = None
    if patient_id.isdigit():
        patient = Patient.objects.filter(pk=int(patient_id)).first()

    patient_ctx = None
    initial_assessment_data = None
    consultation = None
    prescriptions = []
    prescriptions_payload = []
    previous_prescriptions = []
    previous_prescriptions_payload = []
    current_orders = []
    previous_orders = []
    diagnostic_reports = []
    order_banks = []
    extra_orders = []
    secondary_diagnoses_text = ""
    patient_education = []
    last_visit = None
    doctor_name = request.user.get_full_name() or request.user.username
    doctor_specialty = "General Medicine"
    if getattr(request.user, "profile", None) and request.user.profile.specialty:
        doctor_specialty = request.user.profile.get_specialty_display()

    if patient:
        now = timezone.now()

        # Parse patient allergies
        allergies = []
        if patient.allergies:
            allergies = [
                part.strip()
                for part in patient.allergies.replace(";", ",").split(",")
                if part.strip()
            ]

        # Initials
        parts = [p for p in (patient.first_name, patient.last_name) if p]
        initials = "".join(p[0] for p in parts[:2]).upper() or "PT"

        # Generate or load active consultation session. A new encounter is kept
        # unsaved until the doctor saves a draft so browsing the picker does not
        # create empty visits.
        active_appointment = (
            Appointment.objects.filter(
                patient=patient,
                date__gte=timezone.localdate(),
            )
            .filter(status=Appointment.Status.SCHEDULED)
            .order_by("date", "start_time")
            .first()
        )
        consultation = ConsultationSession.objects.filter(
            patient=patient,
            status__in=[
                ConsultationSession.Status.DRAFT,
                ConsultationSession.Status.IN_PROGRESS,
            ],
        ).order_by("-created_at").first()

        if consultation and consultation.appointment_id is None and active_appointment:
            consultation.appointment = active_appointment
            consultation.save(update_fields=["appointment"])

        if not consultation:
            visit_token = f"OPD-{now:%y%m%d}-{patient.pk:04d}"
            consultation = ConsultationSession(
                patient=patient,
                appointment=active_appointment,
                doctor=request.user,
                visit_token=visit_token,
                status=ConsultationSession.Status.DRAFT,
                visit_type=ConsultationSession.VisitType.NEW_PROBLEM,
            )

        latest_patient_ticket = patient.nurse_tickets.filter(
            status=NurseQueueTicket.Status.COMPLETED
        ).order_by("-completed_at", "-sent_at").first()
        triage_reference = (
            latest_patient_ticket.completed_at
            if latest_patient_ticket and latest_patient_ticket.completed_at
            else patient.created_at
        )
        patient_wait_minutes = max(
            0, int((now - triage_reference).total_seconds() // 60)
        )

        patient_ctx = {
            "id": patient.pk,
            "name": patient.full_name or "—",
            "initials": initials,
            "uhid": patient.uhid,
            "visit_token": consultation.visit_token or f"OPD-{now:%y%m%d}-{patient.pk:04d}",
            "age": patient.age if patient.age is not None else "—",
            "gender": patient.get_gender_display() if patient.gender else "—",
            "phone": patient.phone or "—",
            "address": patient.address or "—",
            "blood_group": patient.blood_group or "—",
            "visit_timestamp": timezone.localtime(patient.created_at).strftime("%d %b %Y, %I:%M %p"),
            "department": doctor_specialty,
            "doctor": doctor_name,
            "visit_type": "Emergency" if patient.is_emergency else ("Walk-in" if patient.payment_type != "credit" else "Referred"),
            "referred_by": patient.reference_no or "—",
            "allergies": allergies,
            "chronic_illnesses": [],
            "active_medications": [],
            "is_emergency": bool(patient.is_emergency),
            "has_assessment": False,
            "triage_status": "Awaiting triage",
            "assessment_updated": "",
            "triage_note": "",
            "triage_priority": latest_patient_ticket.priority if latest_patient_ticket else "",
            "triage_priority_display": latest_patient_ticket.get_priority_display() if latest_patient_ticket else "",
            "wait_minutes": patient_wait_minutes,
            "wait_label": _wait_label(patient_wait_minutes),
        }

        # Load initial assessment if exists
        try:
            ia = patient.initial_assessment
        except InitialAssessment.DoesNotExist:
            ia = None

        if ia is not None:
            complaint_items = _parse_complaints(ia.complaints)
            ia_allergies = [
                str(a.get("allergen") if isinstance(a, dict) else a).strip()
                for a in (ia.allergies or [])
                if str(a.get("allergen") if isinstance(a, dict) else a).strip()
            ]
            chronic = _normalise_conditions(ia.chronic_conditions)
            meds = _normalise_medications(ia.medications)
            initial_assessment_data = {
                "temperature": str(ia.temperature) if ia.temperature is not None else "",
                "pulse": ia.pulse if ia.pulse is not None else "",
                "respiratory_rate": ia.respiratory_rate if ia.respiratory_rate is not None else "",
                "bp_systolic": ia.bp_systolic if ia.bp_systolic is not None else "",
                "bp_diastolic": ia.bp_diastolic if ia.bp_diastolic is not None else "",
                "spo2": ia.spo2 if ia.spo2 is not None else "",
                "height": str(ia.height) if ia.height is not None else "",
                "weight": str(ia.weight) if ia.weight is not None else "",
                "bmi": str(ia.bmi) if ia.bmi is not None else "",
                "rbs": ia.rbs if ia.rbs is not None else "",
                "pain_score": ia.pain_score if ia.pain_score is not None else "",
                "complaints": ia.complaints or "",
                "complaint_items": complaint_items,
                "hpi": ia.hpi or "",
                "summary_note": ia.summary_note or "",
                "surgeries": ia.surgeries or "",
                "chronic_conditions": chronic,
                "allergies": ia_allergies,
                "medications": meds,
                "social_history": _normalise_social_history(ia.social_history),
                "examination": ia.examination or {},
                "orders": _normalise_orders(ia.orders),
                "saved": ia.status == InitialAssessment.Status.COMPLETED,
                "status": ia.get_status_display(),
                "updated_at": timezone.localtime(ia.updated_at).strftime("%d %b %Y, %I:%M %p"),
            }

            def _out_of_range(value, low, high):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return False
                return number < low or number > high

            initial_assessment_data["vital_flags"] = {
                "temperature": _out_of_range(ia.temperature, 35.5, 37.5),
                "pulse": _out_of_range(ia.pulse, 60, 100),
                "respiratory_rate": _out_of_range(ia.respiratory_rate, 12, 20),
                "bp": _out_of_range(ia.bp_systolic, 90, 140) or _out_of_range(ia.bp_diastolic, 60, 90),
                "spo2": ia.spo2 is not None and ia.spo2 < 95,
                "rbs": _out_of_range(ia.rbs, 70, 180),
                "pain_score": ia.pain_score is not None and ia.pain_score >= 7,
            }
            patient_ctx["has_assessment"] = True
            patient_ctx["triage_status"] = ia.get_status_display()
            patient_ctx["assessment_updated"] = initial_assessment_data["updated_at"]
            patient_ctx["triage_note"] = ia.summary_note or ""
            patient_ctx["chronic_illnesses"] = [c["name"] for c in chronic]
            patient_ctx["active_medications"] = [
                " ".join(part for part in (m["name"], m["dose"], m["frequency"]) if part)
                for m in meds
            ]
            for item in ia_allergies:
                if item.lower() not in [a.lower() for a in patient_ctx["allergies"]]:
                    patient_ctx["allergies"].append(item)

        # Load active prescriptions for this consultation
        prescriptions = list(consultation.prescriptions.order_by("order_index", "id")) if consultation.pk else []

        _rx_payload = prescription_payload
        prescriptions_payload = [_rx_payload(item) for item in prescriptions]

        # Load current orders for this consultation and mark the matching
        # quick-order checkboxes so the form reopens in the right state.
        current_orders = list(consultation.orders.all()) if consultation.pk else []
        current_order_map = {
            (order.order_type, order.name.casefold()): order
            for order in current_orders
        }
        service_price_map = {
            service.name.casefold(): service.price
            for service in ServiceDefinition.objects.filter(is_active=True)
        }
        matched_order_ids = set()
        for key, title, items in DOCTOR_ORDER_BANKS:
            rows = []
            for name in items:
                existing = current_order_map.get((key, name.casefold()))
                if existing is not None:
                    matched_order_ids.add(existing.pk)
                rows.append({
                    "name": name,
                    "price": service_price_map.get(name.casefold(), 0),
                    "checked": bool(existing and existing.status != "Cancelled"),
                    "status": existing.status if existing else "",
                })
            order_banks.append({"key": key, "title": title, "items": rows})
        extra_orders = [
            {
                "name": order.name,
                "order_type": order.order_type,
                "status": order.status,
                "price": order.price,
                "checked": order.status != "Cancelled",
            }
            for order in current_orders
            if order.pk not in matched_order_ids and order.status != "Cancelled"
        ]

        # Previous prescriptions from older consultations
        older_consultations = ConsultationSession.objects.filter(
            patient=patient
        ).exclude(pk=consultation.pk).order_by("-created_at")

        if older_consultations.exists():
            last_c = older_consultations.first()
            previous_prescriptions = list(last_c.prescriptions.order_by("order_index", "id"))
            previous_prescriptions_payload = [_rx_payload(item) for item in previous_prescriptions]
            previous_orders = list(last_c.orders.all())

        last_completed_consultation = older_consultations.filter(
            status=ConsultationSession.Status.COMPLETED
        ).first()
        if last_completed_consultation:
            last_visit = {
                "date": timezone.localtime(
                    last_completed_consultation.created_at
                ).strftime("%d %b %Y"),
                "diagnosis": last_completed_consultation.diagnosis_primary
                or "No diagnosis recorded",
                "doctor": (
                    last_completed_consultation.doctor.get_full_name()
                    or last_completed_consultation.doctor.username
                )
                if last_completed_consultation.doctor
                else "—",
            }

        # Reports for this patient
        diagnostic_reports = list(accessible_reports(request.user).filter(patient=patient).order_by("-report_date"))

        # Secondary diagnoses may contain strings or dictionaries from older records.
        secondary_labels = []
        for item in (consultation.diagnosis_secondary or []):
            if isinstance(item, dict):
                label = item.get("text") or item.get("name") or item.get("desc") or ""
            else:
                label = str(item)
            if str(label).strip():
                secondary_labels.append(str(label).strip())
        secondary_diagnoses_text = ", ".join(secondary_labels)

        patient_education = []
        for item in (consultation.patient_education or []):
            label = item.get("text") if isinstance(item, dict) else item
            if str(label).strip():
                patient_education.append(str(label).strip())

    return render(request, "core/doctor_consultation.html", {
        "selected_patient": patient,
        "patients": patients,
        "ready_patients": ready_patients,
        "unassigned_ready_patients": unassigned_ready_patients,
        "query": query,
        "patient_ctx": patient_ctx,
        "last_visit": last_visit,
        "assessment": initial_assessment_data,
        "consultation": consultation,
        "prescriptions": prescriptions,
        "prescriptions_payload": prescriptions_payload,
        "current_orders": current_orders,
        "previous_prescriptions": previous_prescriptions,
        "previous_prescriptions_payload": previous_prescriptions_payload,
        "previous_orders": previous_orders,
        "diagnostic_reports": diagnostic_reports,
        "order_banks": order_banks,
        "extra_orders": extra_orders,
        "secondary_diagnoses_text": secondary_diagnoses_text,
        "patient_education": patient_education,
        "education_presets": PATIENT_EDUCATION_PRESETS,
        "doctor_name": doctor_name,
        "doctor_specialty": doctor_specialty,
        "formulary": STANDARD_FORMULARY,
        "service_catalogue": ServiceDefinition.objects.filter(is_active=True).select_related("department"),
        "common_icd": COMMON_ICD10,
        "open_tasks": list(
            patient.nurse_tasks.filter(status__in=["pending", "in_progress"])
            .select_related("assigned_to")
            .order_by("-priority", "due_at", "created_at")[:6]
        ) if patient else [],
        "title": "Doctor Consultation (OPD)",
        "user": request.user,
    })


@require_POST
@login_required(login_url="login")
@guarded_consultation_save
def save_consultation(request):
    """Async endpoint to save consultation session, diagnoses, orders and prescriptions."""
    import json
    from django.utils import timezone
    from patients.models import (
        Patient, ConsultationSession, PrescriptionItem, ClinicalOrder, OPDAuditEvent, ServiceDefinition
    )

    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN):
        return JsonResponse({"ok": False, "error": "Doctor or administrator access required."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON body"}, status=400)

    patient_id = payload.get("patient_id")
    consultation_id = payload.get("consultation_id")
    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return JsonResponse({"ok": False, "error": "Patient not found"}, status=404)

    if consultation_id and str(consultation_id).isdigit():
        consultation = ConsultationSession.objects.filter(pk=consultation_id, patient=patient).first()
    else:
        consultation = None
    if not consultation:
        visit_token = f"OPD-{timezone.now():%y%m%d}-{patient.pk:04d}"
        active_appointment = (
            Appointment.objects.filter(
                patient=patient,
                date__gte=timezone.localdate(),
            )
            .filter(status=Appointment.Status.SCHEDULED)
            .order_by("date", "start_time")
            .first()
        )
        consultation = ConsultationSession.objects.create(
            patient=patient,
            appointment=active_appointment,
            doctor=request.user,
            visit_token=visit_token,
            status=ConsultationSession.Status.DRAFT,
        )
    elif consultation.appointment_id is None:
        active_appointment = (
            Appointment.objects.filter(
                patient=patient,
                date__gte=timezone.localdate(),
            )
            .filter(status=Appointment.Status.SCHEDULED)
            .order_by("date", "start_time")
            .first()
        )
        if active_appointment:
            consultation.appointment = active_appointment
            consultation.save(update_fields=["appointment"])

    if consultation.doctor_id and consultation.doctor_id != request.user.pk and not _is_hospital_admin(request):
        return JsonResponse({"ok": False, "error": "This consultation belongs to another doctor."}, status=403)
    action = payload.get("action", "save_draft")
    if action not in ("save_draft", "draft", "complete", "cancel", "send_pharmacy"):
        return JsonResponse({"ok": False, "error": "Invalid consultation action."}, status=400)
    if action == "cancel" and consultation.prescriptions.filter(released_at__isnull=False).exclude(dispense_status="cancelled").exists():
        return JsonResponse({"ok": False, "error": "Cancel the unissued prescription balances before cancelling this consultation."}, status=400)
    if action == "complete":
        consultation.status = ConsultationSession.Status.COMPLETED
    elif action == "cancel":
        consultation.status = ConsultationSession.Status.CANCELLED
    elif action != "send_pharmacy":
        consultation.status = ConsultationSession.Status.DRAFT

    # Update clinical data
    consultation.visit_type = payload.get("visit_type", ConsultationSession.VisitType.NEW_PROBLEM)
    consultation.clinical_note = payload.get("clinical_note", "") or ""
    consultation.examination = payload.get("examination", {}) or {}
    consultation.diagnosis_primary = payload.get("diagnosis_primary", "") or ""
    consultation.diagnosis_primary_icd = payload.get("diagnosis_primary_icd", "") or ""
    consultation.diagnosis_secondary = payload.get("diagnosis_secondary", []) or []
    consultation.treatment_plan = payload.get("treatment_plan", "") or ""
    consultation.special_advice = payload.get("special_advice", "") or ""
    consultation.patient_education = payload.get("patient_education", []) or []
    consultation.mark_follow_up = bool(payload.get("mark_follow_up", False))
    consultation.follow_up_after = payload.get("follow_up_after", "") or ""
    follow_up_days = {
        "3 days": 3,
        "5 days": 5,
        "1 week": 7,
        "2 weeks": 14,
        "1 month": 30,
    }
    if consultation.mark_follow_up and consultation.follow_up_after in follow_up_days:
        from datetime import timedelta
        consultation.follow_up_date = timezone.localdate() + timedelta(
            days=follow_up_days[consultation.follow_up_after]
        )
    else:
        consultation.follow_up_date = None
    consultation.additional_orders = payload.get("additional_orders", []) or []

    # Attribute the saved encounter to the doctor using the workstation.
    if not consultation.doctor_id:
        consultation.doctor = request.user
    consultation.save()

    if "prescriptions" in payload or action == "send_pharmacy":
        sync_prescriptions(
            consultation,
            payload.get(
                "prescriptions",
                [prescription_payload(rx) for rx in consultation.prescriptions.all()],
            ),
            release=action in ("send_pharmacy", "complete"),
        )

    # Update orders
    orders_list = payload.get("orders")
    if isinstance(orders_list, list):
        existing_orders = list(consultation.orders.all())
        retained_orders = set()
        for ord_item in orders_list:
            ord_name = ord_item.get("name", "").strip()
            if ord_name:
                ord_type = ord_item.get("order_type", "laboratory")
                match = next((existing for existing in existing_orders
                              if existing.pk not in retained_orders and existing.status != "Cancelled"
                              and existing.order_type == ord_type
                              and existing.name.casefold() == ord_name.casefold()), None)
                if match:
                    retained_orders.add(match.pk)
                    if match.status == "Cancelled":
                        match.status = "Ordered"
                        match.updated_by = request.user
                        if not match.price and ord_item.get("price"):
                            match.price = ord_item.get("price")
                        match.save(update_fields=["status", "price", "updated_by", "updated_at"])
                else:
                    created = ClinicalOrder.objects.create(
                        consultation=consultation,
                        order_type=ord_type,
                        name=ord_name,
                        status="Ordered",
                        result_summary=ord_item.get("result_summary", ""),
                        price=ord_item.get("price") or 0,
                        updated_by=request.user,
                        service_definition=ServiceDefinition.objects.filter(pk=ord_item.get("service_id"), is_active=True).first() if ord_item.get("service_id") else None,
                        priority="urgent" if ord_item.get("priority") == "urgent" else "routine",
                    )
                    retained_orders.add(created.pk)
                    # A nursing procedure becomes one task tied to this order.
                    from core.nursing import ensure_task_for_order
                    ensure_task_for_order(created, request.user)
        for existing in existing_orders:
            if existing.pk not in retained_orders and existing.status not in ("Completed", "Cancelled"):
                previous_status = existing.status
                existing.status = "Cancelled"
                existing.updated_by = request.user
                existing.save(update_fields=["status", "updated_by", "updated_at"])
                OPDAuditEvent.objects.create(
                    patient=patient,
                    order=existing,
                    actor=request.user,
                    action="service_order_cancelled",
                    summary=f"{existing.name}: {previous_status} to Cancelled",
                    details={"previous_status": previous_status, "status": "Cancelled"},
                )

    sync_service_bills(user=request.user, consultation=consultation)
    if action == "complete" and consultation.appointment_id:
        Appointment.objects.filter(pk=consultation.appointment_id).update(
            status=Appointment.Status.COMPLETED
        )

    OPDAuditEvent.objects.create(
        patient=patient,
        actor=request.user,
        action=f"consultation_{consultation.status}",
        summary=f"Consultation {consultation.get_status_display().lower()} by {request.user.get_full_name() or request.user.username}",
        details={"consultation_id": consultation.pk, "status": consultation.status},
    )

    return JsonResponse({
        "ok": True,
        "id": consultation.pk,
        "status": consultation.status,
        "status_display": consultation.get_status_display(),
        "prescriptions": [prescription_payload(rx) for rx in consultation.prescriptions.order_by("order_index", "id")],
    })


@login_required(login_url="login")
def drug_search_api(request):
    """API endpoint to search the medicine formulary with category filtering."""
    query = request.GET.get("q", "").strip().lower()
    category = request.GET.get("cat", "").strip()

    from patients.models import Medicine
    results = [{"id": m.pk, "name": m.name, "generic": m.generic_name, "dose": m.strength, "route": "Oral", "category": m.form, "unit": m.unit} for m in Medicine.objects.filter(is_active=True).filter(Q(name__icontains=query) | Q(generic_name__icontains=query))[:25]]
    for drug in STANDARD_FORMULARY:
        match_query = (not query) or (query in drug["name"].lower()) or (query in drug["generic"].lower())
        match_cat = (not category) or (category.lower() == "common") or (category.lower() in drug["category"].lower())
        if match_query and match_cat:
            if not any(d["name"] == drug["name"] for d in results):
                results.append(drug)

    return JsonResponse({"ok": True, "drugs": results[:25]})


@login_required(login_url="login")
def icd10_search_api(request):
    """API endpoint to search ICD-10 diagnosis codes."""
    query = request.GET.get("q", "").strip().lower()
    results = []
    for item in COMMON_ICD10:
        if (not query) or (query in item["code"].lower()) or (query in item["desc"].lower()):
            results.append(item)
    return JsonResponse({"ok": True, "results": results[:20]})


@require_POST
@login_required(login_url="login")
def safety_check_api(request):
    """Real-time clinical safety engine checking drug-allergy and drug-drug interactions."""
    import json
    from patients.models import Patient

    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN):
        return JsonResponse({"ok": False, "error": "Doctor access required."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    patient_id = payload.get("patient_id")
    medicines = payload.get("medicines", [])
    patient = Patient.objects.filter(pk=patient_id).first()

    allergy_alerts = []
    interaction_alerts = []

    patient_allergies = []
    if patient and patient.allergies:
        patient_allergies = [a.lower().strip() for a in patient.allergies.replace(";", ",").split(",") if a.strip()]

    # Drug allergy checks
    for med in medicines:
        med_lower = med.lower() if isinstance(med, str) else med.get("medicine_name", "").lower()
        for allergen in patient_allergies:
            if "penicillin" in allergen and any(k in med_lower for k in ["penicillin", "amox", "ampicillin", "augmentin", "clav"]):
                allergy_alerts.append(f"Allergy Alert: {allergen.title()} — Avoid Penicillin and related beta-lactam antibiotics ({med}).")
            elif "sulfa" in allergen and any(k in med_lower for k in ["sulfa", "cotrimoxazole", "bactrim"]):
                allergy_alerts.append(f"Allergy Alert: {allergen.title()} — Avoid Sulfa-containing medications ({med}).")
            elif "nsaid" in allergen or "aspirin" in allergen:
                if any(k in med_lower for k in ["ibuprofen", "diclofenac", "aspirin", "naproxen"]):
                    allergy_alerts.append(f"Allergy Alert: {allergen.title()} — Potential cross-reactivity with NSAID ({med}).")

    return JsonResponse({
        "ok": True,
        "has_alerts": bool(allergy_alerts or interaction_alerts),
        "allergy_alerts": list(set(allergy_alerts)),
        "interaction_alerts": interaction_alerts,
    })



# ── Triage workstation pages (nurse desk merged into the OPD Desk) ─────────────
#
# The OPD assistant records the initial assessment. Visit routing:
#   OPD Desk  ──send_to_nurse()──▶  ticket claimed → triage workstation (IN_ASSESSMENT)
#   OPD Desk  ──save (complete)──▶  COMPLETED  → back at OPD Desk for doctor allocation
#   OPD Desk  ──recall──▶           RECALLED   → returns to the triage queue
#   Emergency / walk-in cases open a ticket directly at the triage workstation,
#   and the save step still returns them to the OPD Desk.

#: Quick triage complaint chips shown on the nurse workstation.
NURSE_COMPLAINT_CHIPS = [
    "Fever", "Cough", "Cold / Running nose", "Sore throat", "Headache",
    "Body ache", "Chest pain", "Breathlessness", "Abdominal pain", "Vomiting",
    "Loose motions", "Burning micturition", "Giddiness", "Weakness / Fatigue",
    "Injury / Trauma", "Skin rash",
]

#: Emergency red flags offered as one-tap toggles during triage.
NURSE_RED_FLAGS = [
    ("chest_pain", "Chest pain", "possible cardiac event"),
    ("breathlessness", "Severe breathlessness / SpO₂ < 92%", "respiratory distress"),
    ("neuro_deficit", "Focal deficit / slurred speech / weakness", "possible stroke"),
    ("uncontrolled_bleeding", "Uncontrolled bleeding", "haemorrhage risk"),
    ("altered_sensorium", "Altered sensorium / unconscious", "neuro emergency"),
    ("severe_pain", "Severe pain (score ≥ 8)", "pain crisis"),
    ("high_fever", "High fever ≥ 103 °F with rigors", "sepsis risk"),
    ("pregnancy_emergency", "Pregnancy / obstetric emergency", "obstetric emergency"),
]

#: ESI-style response bands derived from the priority score.
NURSE_PRIORITY_BANDS = [
    (0, 39, "Routine", "Green", "Send back in the normal doctor queue."),
    (40, 69, "Semi-urgent", "Amber", "Fast-track ahead of routine visits."),
    (70, 100, "Emergency", "Red", "Call the duty doctor immediately and keep the patient under observation."),
]

#: One-tap test/procedure banks the nurse can pre-order before the doctor sees
#: the patient (keyed by ClinicalOrder.order_type).
NURSE_ORDER_BANKS = [
    ("laboratory", "Laboratory", [
        "CBC", "ESR", "Blood sugar (RBS/FBS)", "HbA1c", "RFT", "LFT",
        "Urine routine", "Urine culture", "Serum electrolytes", "CRP", "Dengue NS1",
    ]),
    ("radiology", "Radiology", [
        "Chest X-Ray PA", "USG Abdomen", "ECG", "2D Echo", "X-Ray Limb", "CT Head",
    ]),
    ("other", "Nursing / procedures", [
        "Nebulisation", "IV fluids", "Injection (as advised)", "Dressing", "Nebuliser refill",
    ]),
]

#: Nursing task quick-start presets (kind, title).
NURSE_TASK_PRESETS = [
    ("vitals", "Check vitals now"),
    ("injection", "Injection as prescribed"),
    ("dressing", "Wound dressing"),
    ("sample", "Collect sample for laboratory"),
    ("monitoring", "15-min observation round"),
    ("medication", "Give medication / nebulisation"),
]

NURSE_OPEN_STATUSES = [
    "waiting", "in_assessment", "recalled",
]


def _designation_of(request):
    return normalize_designation(
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )


def _can_work_nurse_task(request, task):
    """OPD, doctors and admins work any task. Service staff only their linked order."""
    if _require_nurse(request) is None:
        return True
    if _designation_of(request) != UserProfile.Designation.SERVICE_STAFF or not task.order_id:
        return False
    department_id = task.order.department_id
    if not department_id:
        return False
    return request.user.profile.service_departments.filter(pk=department_id).exists()


def _require_nurse(request):
    """Return a redirect when the signed-in account may not use the triage desk.

    The former standalone Nurse designation was merged into OPD Assistant, so
    the triage workstation pages belong to the OPD Desk (doctors and admins
    keep the access they already had).
    """
    if getattr(request.user, "is_superuser", False):
        return None
    designation = _designation_of(request)
    if designation in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.ADMIN,
        UserProfile.Designation.DOCTOR,
    ):
        return None
    messages.warning(request, "Access restricted: the triage desk is for OPD Assistants, Doctors and administrators.")
    if designation == UserProfile.Designation.REGISTRATION_DESK:
        return redirect("reception_dashboard")
    return redirect("home")


def _default_priority_for(patient, appointment=None):
    """Emergency registrations and marked appointments open as emergency/urgent."""
    from patients.models import NurseQueueTicket
    if patient.is_emergency:
        return NurseQueueTicket.Priority.EMERGENCY
    if appointment is not None and getattr(appointment, "status", "") == "urgent":
        return NurseQueueTicket.Priority.URGENT
    return NurseQueueTicket.Priority.ROUTINE


def _triage_priority_score(assessment):
    """ESI-style score (0-100) from the stored vitals, pain and red flags.

    Higher = more urgent. The bands are returned by ``_priority_band``.
    """
    score = 0

    def _flag(field):
        return bool(((assessment.examination or {}).get("red_flags") or {}).get(field))

    for key, _, _ in NURSE_RED_FLAGS:
        if _flag(key):
            score += 20
    if _flag("chest_pain") or _flag("breathlessness") or _flag("neuro_deficit"):
        score += 25

    spo2 = assessment.spo2
    if spo2 is not None:
        if spo2 <= 90:
            score += 40
        elif spo2 <= 94:
            score += 25
        elif spo2 <= 96:
            score += 10
    if assessment.bp_systolic is not None:
        if assessment.bp_systolic < 90 or assessment.bp_systolic > 180:
            score += 25
        elif assessment.bp_systolic > 160:
            score += 10
    if assessment.bp_diastolic is not None and assessment.bp_diastolic > 110:
        score += 15
    if assessment.pulse is not None:
        if assessment.pulse > 130 or assessment.pulse < 50:
            score += 25
        elif assessment.pulse > 110:
            score += 12
    if assessment.respiratory_rate is not None:
        if assessment.respiratory_rate > 30 or assessment.respiratory_rate < 10:
            score += 25
        elif assessment.respiratory_rate > 24:
            score += 12
    if assessment.temperature is not None:
        temp = float(assessment.temperature)
        if temp >= 39.5:
            score += 20
        elif temp >= 38.5:
            score += 12
        elif temp < 35:
            score += 20
    if assessment.pain_score is not None:
        if assessment.pain_score >= 8:
            score += 20
        elif assessment.pain_score >= 5:
            score += 10
    if assessment.rbs is not None:
        if assessment.rbs < 60 or assessment.rbs > 350:
            score += 20
        elif assessment.rbs > 250:
            score += 8
    return min(int(score), 100)


def _priority_band(score):
    for low, high, label, colour, guidance in NURSE_PRIORITY_BANDS:
        if low <= score <= high:
            return {"label": label, "colour": colour, "guidance": guidance, "score": score}
    return {"label": "Routine", "colour": "Green", "guidance": "", "score": score}


def _nurse_vitals_alerts(assessment):
    """Human readable out-of-range alerts for the nurse strip + doctor handover."""
    alerts = []
    if not assessment:
        return alerts
    if assessment.spo2 is not None and assessment.spo2 < 92:
        alerts.append(f"Low SpO₂ {assessment.spo2}%")
    if assessment.pulse is not None and assessment.pulse > 120:
        alerts.append(f"Tachycardia {assessment.pulse}/min")
    if assessment.pulse is not None and assessment.pulse < 50:
        alerts.append(f"Bradycardia {assessment.pulse}/min")
    if assessment.bp_systolic is not None and assessment.bp_systolic >= 160:
        alerts.append(f"High BP {assessment.bp_systolic}/{assessment.bp_diastolic or '—'}")
    if assessment.bp_systolic is not None and assessment.bp_systolic < 90:
        alerts.append(f"Low BP {assessment.bp_systolic}/{assessment.bp_diastolic or '—'}")
    if assessment.temperature is not None and float(assessment.temperature) >= 39.5:
        alerts.append(f"High fever {assessment.temperature}°C")
    if assessment.respiratory_rate is not None and assessment.respiratory_rate > 24:
        alerts.append(f"High respiratory rate {assessment.respiratory_rate}/min")
    if assessment.rbs is not None and (assessment.rbs < 60 or assessment.rbs > 350):
        alerts.append(f"Abnormal RBS {assessment.rbs} mg/dL")
    return alerts


def _nurse_ticket_for_patient(patient):
    """The newest still-open triage ticket for a patient, if any."""
    from patients.models import NurseQueueTicket
    return (
        patient.nurse_tickets.filter(status__in=NURSE_OPEN_STATUSES)
        .select_related("claimed_by", "sent_by")
        .order_by("-sent_at")
        .first()
    )


def _nurse_priority_token(ticket):
    """Queue sort token: emergency first, then urgent, then sent time."""
    rank = {"emergency": 0, "urgent": 1, "routine": 2}.get(ticket.priority, 3)
    return (rank, ticket.sent_at or timezone.now())


def _latest_vitals(patient):
    try:
        return patient.initial_assessment
    except Exception:  # noqa: BLE001 — missing assessment is a normal state
        return None


@login_required(login_url="login")
def nurse_portal(request):
    """Legacy nurse-portal home — merged into the OPD Assistant dashboard.

    The OPD Assistant dashboard is now the single desk home; the triage queue,
    nursing tasks and handover sheets are its tabs. ``/nurse/`` simply forwards
    there.
    """
    denied = _require_nurse(request)
    if denied:
        return denied
    return redirect("opd_assistant_dashboard")


@login_required(login_url="login")
def nurse_queue(request):
    """The triage queue itself: search, filter and prioritise incoming visits."""
    from patients.models import NurseQueueTicket

    denial = _require_nurse(request)
    if denial:
        return denial

    status_filter = request.GET.get("status", "open")
    priority_filter = request.GET.get("priority", "")
    query = request.GET.get("q", "").strip()

    base = NurseQueueTicket.objects.select_related(
        "patient", "claimed_by", "sent_by", "assessment",
    )
    today = timezone.localdate()
    if status_filter == "open":
        tickets = list(base.filter(status__in=NURSE_OPEN_STATUSES))
    elif status_filter == "awaiting":
        tickets = list(base.filter(status__in=[NurseQueueTicket.Status.WAITING, NurseQueueTicket.Status.RECALLED]))
    elif status_filter == "today":
        tickets = list(base.filter(sent_at__date=today))
    elif status_filter in NurseQueueTicket.Status.values:
        tickets = list(base.filter(status=status_filter))
    else:
        tickets = list(base.all()[:200])
    tickets.sort(key=_nurse_priority_token)

    if priority_filter == "urgent_all":
        tickets = [t for t in tickets if t.priority in (NurseQueueTicket.Priority.EMERGENCY, NurseQueueTicket.Priority.URGENT)]
    elif priority_filter in NurseQueueTicket.Priority.values:
        tickets = [t for t in tickets if t.priority == priority_filter]
    if query:
        needle = query.casefold()
        tickets = [
            t for t in tickets
            if needle in " ".join((
                t.patient.full_name, t.patient.uhid or "", t.ticket_number,
                t.patient.phone or "",
            )).casefold()
        ]

    return render(request, "core/nurse_queue.html", {
        "today": today,
        "tickets": tickets,
        "status_filter": status_filter,
        "priority_filter": priority_filter,
        "query": query,
        "counts": {
            "open": base.filter(status__in=NURSE_OPEN_STATUSES).count(),
            "waiting": base.filter(status=NurseQueueTicket.Status.WAITING).count(),
            "emergency": base.filter(
                priority__in=[NurseQueueTicket.Priority.EMERGENCY, NurseQueueTicket.Priority.URGENT],
                status__in=NURSE_OPEN_STATUSES,
            ).count(),
            "completed_today": base.filter(
                status=NurseQueueTicket.Status.COMPLETED, completed_at__date=today
            ).count(),
        },
    })


@login_required(login_url="login")
def nurse_triage(request):
    """Nurse triage workstation — record vitals, complaints, flags and hand over.

    ``?ticket=<id>`` opens a queued visit (and claims it for this nurse);
    ``?patient=<id>`` opens a walk-in patient that arrived directly at the
    nurse desk (a ticket is created in that case so the visit is traceable).
    """
    from patients.models import InitialAssessment, NurseQueueTicket, OPDAuditEvent

    denied = _require_nurse(request)
    if denied:
        return denied

    ticket = None
    patient = None
    ticket_id = request.GET.get("ticket", "").strip()
    patient_id = request.GET.get("patient", "").strip()

    if ticket_id.isdigit():
        ticket = NurseQueueTicket.objects.select_related("patient", "sent_by").filter(pk=int(ticket_id)).first()
        patient = ticket.patient if ticket else None
    elif patient_id.isdigit():
        patient = Patient.objects.filter(pk=int(patient_id)).first()
        if patient:
            ticket = _nurse_ticket_for_patient(patient)

    if patient and (ticket is None or not ticket.is_open):
        # Walk-in / emergency drop-in: open a fresh ticket so the OPD Desk can
        # see the visit when the assessment is saved.
        active_appointment = (
            Appointment.objects.filter(
                patient=patient,
                date__gte=timezone.localdate(),
            )
            .filter(status=Appointment.Status.SCHEDULED)
            .order_by("date", "start_time")
            .first()
        )
        ticket = NurseQueueTicket.objects.create(
            patient=patient,
            appointment=active_appointment,
            status=NurseQueueTicket.Status.WAITING,
            priority=_default_priority_for(patient),
            source=(
                NurseQueueTicket.Source.EMERGENCY if patient.is_emergency
                else NurseQueueTicket.Source.REGISTRATION
            ),
            visit_note="Walk-in at nurse desk",
            sent_by=request.user,
        )
        OPDAuditEvent.objects.create(
            patient=patient,
            actor=request.user,
            action="nurse_ticket_opened",
            summary=f"Walk-in triage ticket {ticket.ticket_number} opened at the nurse desk",
            details={"ticket_id": ticket.pk},
        )

    claimed_now = False
    if ticket and ticket.is_open and ticket.status == NurseQueueTicket.Status.WAITING:
        claimed_now = bool(NurseQueueTicket.objects.filter(
            pk=ticket.pk, status=NurseQueueTicket.Status.WAITING
        ).update(status=NurseQueueTicket.Status.IN_ASSESSMENT, claimed_by=request.user, claimed_at=timezone.now()))
        ticket.refresh_from_db()
        if not claimed_now and ticket.claimed_by_id != request.user.pk:
            messages.warning(request, "This visit was already claimed or is no longer open. Review the queue.")
            return redirect("nurse_queue")

    queue_patients = list(
        NurseQueueTicket.objects.filter(status=NurseQueueTicket.Status.WAITING)
        .select_related("patient")
        .order_by("-priority", "sent_at")[:20]
    )
    if ticket is not None:
        queue_patients = [t for t in queue_patients if t.pk != ticket.pk]
    queue_patients.sort(key=_nurse_priority_token)

    patient_ctx = None
    assessment_data = {
        "temperature": "", "pulse": "", "respiratory_rate": "",
        "bp_systolic": "", "bp_diastolic": "", "spo2": "",
        "height": "", "weight": "", "bmi": "", "rbs": "", "pain_score": "",
        "complaints": [], "hpi": "", "summary_note": "", "surgeries": "",
        "chronic_conditions": [], "conditions": [], "custom_conditions": [],
        "allergies": [], "medications": [], "social_history": [],
        "examination": {"general": "", "cvs": "", "respiratory": "", "abdomen": "", "cns": "", "red_flags": {}},
        "orders": {"laboratory": [], "radiology": [], "other": []},
        "medications_text": "",
        "saved": False,
    }
    vitals_status = None
    band = _priority_band(0)
    recent_tickets = []

    if ticket:
        patient = ticket.patient
        now = timezone.now()
        parts = [p for p in (patient.first_name, patient.last_name) if p]
        initials = "".join(p[0] for p in parts[:2]).upper() or "PT"

        patient_ctx = {
            "id": patient.pk,
            "name": patient.full_name or "—",
            "initials": initials,
            "uhid": patient.uhid,
            "age": patient.age if patient.age is not None else "—",
            "gender": patient.get_gender_display() if patient.gender else "—",
            "phone": patient.phone or "—",
            "blood_group": patient.blood_group or "—",
            "visit_timestamp": timezone.localtime(patient.created_at).strftime("%d %b %Y, %I:%M %p"),
            "is_emergency": bool(patient.is_emergency),
            "allergies_text": patient.allergies or "",
            "ticket": ticket,
        }

        a = None
        try:
            a = patient.initial_assessment
        except InitialAssessment.DoesNotExist:
            a = None

        if a is not None:
            vitals_status = a.status
            assessment_data.update({
                "temperature": "" if a.temperature is None else str(a.temperature),
                "pulse": "" if a.pulse is None else str(a.pulse),
                "respiratory_rate": "" if a.respiratory_rate is None else str(a.respiratory_rate),
                "bp_systolic": "" if a.bp_systolic is None else str(a.bp_systolic),
                "bp_diastolic": "" if a.bp_diastolic is None else str(a.bp_diastolic),
                "spo2": "" if a.spo2 is None else str(a.spo2),
                "height": "" if a.height is None else str(a.height),
                "weight": "" if a.weight is None else str(a.weight),
                "bmi": "" if a.bmi is None else str(a.bmi),
                "rbs": "" if a.rbs is None else str(a.rbs),
                "pain_score": "" if a.pain_score is None else str(a.pain_score),
                "complaints": _parse_complaints(a.complaints),
                "hpi": a.hpi or "",
                "summary_note": a.summary_note or "",
                "surgeries": a.surgeries or "",
                "allergies": [str(x) for x in (a.allergies or [])],
                "medications": _normalise_medications(a.medications),
                "social_history": _normalise_social_history(a.social_history),
                "examination": a.examination or {},
                "orders": _normalise_orders(a.orders),
                "chronic_conditions": _normalise_conditions(a.chronic_conditions),
                "saved": a.status == InitialAssessment.Status.COMPLETED,
            })
            assessment_data["medications_text"] = "\n".join(
                (f"{m['name']} {m['dose']} {m['frequency']}").strip()
                for m in assessment_data["medications"]
                if m.get("name")
            )
            assessment_data["examination"].setdefault("red_flags", {})
            assessment_data["examination"].setdefault("nurse_observations", {})
            band = _priority_band(_triage_priority_score(a))

        remaining = list(assessment_data["chronic_conditions"])
        conditions = []
        for key, label in CHRONIC_CONDITION_OPTIONS:
            match = next((c for c in remaining if c["name"].lower() == label.lower()), None)
            if match:
                remaining.remove(match)
            conditions.append({
                "key": key, "label": label,
                "checked": bool(match),
                "since": match["since"] if match else "",
            })
        assessment_data["conditions"] = conditions
        assessment_data["custom_conditions"] = remaining

        merged = []
        if patient.allergies:
            merged = [
                part.strip() for part in patient.allergies.replace(";", ",").split(",")
                if part.strip()
            ]
        for item in assessment_data["allergies"]:
            if item.lower() not in [m.lower() for m in merged]:
                merged.append(item)
        assessment_data["allergies"] = merged

        recent_tickets = list(
            patient.nurse_tickets.select_related("completed_by", "claimed_by").order_by("-sent_at")[:5]
        )

    # Template membership test (``item in checked_orders``) — never leave this
    # empty, or Django renders the literal string "[]" and every bank item
    # would match. ``"none"`` can never equal a real test name.
    checked_orders = [
        name for names in assessment_data["orders"].values() for name in names
    ] or ["none"]

    # JSON-safe copies for the inline JS bootstrap (Python repr of dicts/bools
    # is not valid JavaScript, e.g. True/False).
    assessment_data["orders_json"] = _json.dumps(assessment_data.get("orders") or {})
    assessment_data["social_history_json"] = _json.dumps(assessment_data.get("social_history") or [])
    _examination = assessment_data.get("examination") or {}
    assessment_data["red_flags_json"] = _json.dumps(_examination.get("red_flags") or {})

    return render(request, "core/nurse_triage.html", {
        "ticket": ticket,
        "claimed_now": claimed_now,
        "patient_ctx": patient_ctx,
        "selected_patient": patient,
        "has_patient": patient is not None,
        "assessment": assessment_data,
        "vitals_status": vitals_status,
        "band": band,
        "condition_options": CHRONIC_CONDITION_OPTIONS,
        "duration_options": DURATION_OPTIONS,
        "complaint_chips": NURSE_COMPLAINT_CHIPS,
        "red_flags": NURSE_RED_FLAGS,
        "order_banks": NURSE_ORDER_BANKS,
        "checked_orders": checked_orders,
        "queue_patients": queue_patients,
        "recent_tickets": recent_tickets,
        "today": timezone.localdate(),
    })


@login_required(login_url="login")
@require_POST
def nurse_send_to_queue(request):
    """OPD Desk: start the initial assessment for a visit.

    Creates (or reopens) the triage ticket, claims it for the signed-in
    assistant and drops them straight into the triage workstation — the
    former two-step handoff through the nurse queue is gone.
    """
    from patients.models import NurseQueueTicket, OPDAuditEvent

    designation = _designation_of(request)
    if not request.user.is_superuser and designation not in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.ADMIN,
    ):
        return HttpResponseForbidden("OPD Desk access required.")

    patient = get_object_or_404(Patient, pk=request.POST.get("patient_id") or 0)
    priority = request.POST.get("priority", NurseQueueTicket.Priority.ROUTINE)
    if priority not in NurseQueueTicket.Priority.values:
        priority = NurseQueueTicket.Priority.ROUTINE
    note = request.POST.get("note", "").strip()[:255]
    appointment_id = request.POST.get("appointment_id", "").strip()
    appointment = None
    if appointment_id.isdigit():
        appointment = Appointment.objects.filter(pk=int(appointment_id), patient=patient).first()
    if appointment is None:
        appointment = (
            Appointment.objects.filter(
                patient=patient,
                date__gte=timezone.localdate(),
            )
            .filter(status=Appointment.Status.SCHEDULED)
            .order_by("date", "start_time")
            .first()
        )

    if (
        appointment is not None
        and appointment_pre_visit_pending(appointment)
        and not patient.is_emergency
        and request.POST.get("override") != "1"
    ):
        messages.error(
            request,
            f"{patient.full_name} still has a pending registration/appointment bill. "
            "Collect payment at the registration desk before starting the assessment.",
        )
        next_url = request.POST.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect("opd_patient_queue")

    existing = _nurse_ticket_for_patient(patient)
    if existing:
        ticket = existing
        messages.info(
            request,
            f"{patient.full_name} is already in the triage queue as {ticket.ticket_number}.",
        )
    else:
        ticket = NurseQueueTicket.objects.create(
            patient=patient,
            appointment=appointment,
            priority=priority,
            source=NurseQueueTicket.Source.OPD_DESK,
            visit_note=note,
            sent_by=request.user,
        )
        OPDAuditEvent.objects.create(
            patient=patient,
            actor=request.user,
            action="nurse_sent",
            summary=f"Assessment started — {ticket.ticket_number} ({priority})",
            details={"ticket_id": ticket.pk, "priority": priority, "note": note},
        )
        messages.success(
            request,
            f"{patient.full_name} assessment started as {ticket.ticket_number}.",
        )

    # The assistant who starts the assessment records it: claim the ticket and
    # jump straight into the triage workstation instead of leaving it in the
    # queue for a second handoff.
    claimed = NurseQueueTicket.objects.filter(pk=ticket.pk).filter(
        Q(status__in=[NurseQueueTicket.Status.WAITING, NurseQueueTicket.Status.RECALLED])
        | Q(status=NurseQueueTicket.Status.IN_ASSESSMENT, claimed_by__isnull=True)
    ).update(status=NurseQueueTicket.Status.IN_ASSESSMENT, claimed_by=request.user, claimed_at=timezone.now())
    if not claimed:
        ticket.refresh_from_db()
        if ticket.status != NurseQueueTicket.Status.IN_ASSESSMENT or ticket.claimed_by_id != request.user.pk:
            messages.warning(request, "This visit was already claimed or is no longer open. Review the queue.")
            return redirect("nurse_queue")
    if existing:
        NurseQueueTicket.objects.filter(
            pk=ticket.pk, status=NurseQueueTicket.Status.IN_ASSESSMENT, claimed_by=request.user
        ).update(priority=priority, visit_note=note or existing.visit_note,
                 appointment=appointment or existing.appointment)
    return redirect(f"{reverse('nurse_triage')}?ticket={ticket.pk}")


@login_required(login_url="login")
@require_POST
def nurse_claim_ticket(request, ticket_id):
    """Take ownership of a triage ticket (Waiting/Recalled → In assessment)."""
    from patients.models import NurseQueueTicket, OPDAuditEvent

    denied = _require_nurse(request)
    if denied:
        return denied
    ticket = get_object_or_404(NurseQueueTicket, pk=ticket_id)
    claimed = NurseQueueTicket.objects.filter(pk=ticket.pk).filter(
        Q(status__in=[NurseQueueTicket.Status.WAITING, NurseQueueTicket.Status.RECALLED])
        | Q(status=NurseQueueTicket.Status.IN_ASSESSMENT, claimed_by__isnull=True)
    ).update(status=NurseQueueTicket.Status.IN_ASSESSMENT, claimed_by=request.user, claimed_at=timezone.now())
    if claimed:
        OPDAuditEvent.objects.create(
            patient=ticket.patient, actor=request.user, action="nurse_claimed",
            summary=f"{ticket.ticket_number} claimed for assessment",
            details={"ticket_id": ticket.pk},
        )
        messages.success(request, f"{ticket.ticket_number} claimed — start the assessment.")
        return redirect(f"{reverse('nurse_triage')}?ticket={ticket.pk}")
    ticket.refresh_from_db()
    if ticket.status == NurseQueueTicket.Status.IN_ASSESSMENT and ticket.claimed_by_id == request.user.pk:
        return redirect(f"{reverse('nurse_triage')}?ticket={ticket.pk}")
    messages.warning(request, "This visit was already claimed or is no longer open. Review the queue.")
    return redirect("nurse_queue")


@login_required(login_url="login")
@require_POST
def nurse_recall_ticket(request, ticket_id):
    """Reopen a completed/recallable visit for a repeat vitals round."""
    from patients.models import NurseQueueTicket, OPDAuditEvent

    denied = _require_nurse(request)
    if denied:
        return denied
    ticket = get_object_or_404(NurseQueueTicket, pk=ticket_id)
    if ticket.status not in (
        NurseQueueTicket.Status.COMPLETED, NurseQueueTicket.Status.RECALLED,
    ):
        messages.warning(request, "Only assessed visits can be recalled.")
        return redirect("nurse_queue")

    ticket.status = NurseQueueTicket.Status.RECALLED
    ticket.returned_at = None
    ticket.claimed_by = None
    ticket.claimed_at = None
    ticket.save(update_fields=["status", "returned_at", "claimed_by", "claimed_at"])
    OPDAuditEvent.objects.create(
        patient=ticket.patient,
        actor=request.user,
        action="nurse_recalled",
        summary=f"{ticket.ticket_number} recalled for re-assessment",
        details={"ticket_id": ticket.pk, "reason": request.POST.get("reason", "")[:200]},
    )
    messages.success(request, f"{ticket.ticket_number} recalled to the nurse queue.")
    return redirect("nurse_queue")


@login_required(login_url="login")
@require_POST
def nurse_cancel_ticket(request, ticket_id):
    """Cancel a queue ticket (duplicate / patient left / re-routed)."""
    from patients.models import NurseQueueTicket, OPDAuditEvent

    denied = _require_nurse(request)
    if denied:
        return denied
    ticket = get_object_or_404(NurseQueueTicket, pk=ticket_id)
    ticket.status = NurseQueueTicket.Status.CANCELLED
    ticket.response_notes = request.POST.get("reason", "").strip()[:500]
    ticket.save(update_fields=["status", "response_notes"])
    OPDAuditEvent.objects.create(
        patient=ticket.patient,
        actor=request.user,
        action="nurse_ticket_cancelled",
        summary=f"{ticket.ticket_number} cancelled",
        details={"ticket_id": ticket.pk, "reason": ticket.response_notes},
    )
    messages.warning(request, f"{ticket.ticket_number} cancelled.")
    return redirect("nurse_queue")


@login_required(login_url="login")
@require_POST
def nurse_save_triage(request):
    """Save the nurse assessment and route the visit back to the OPD Desk.

    Accepts the same clinical payload as ``save_initial_assessment`` plus the
    nurse extras (priority, red flags, observations, ETA, escalation). The
    triage priority score is computed server-side and stored with the visit so
    the OPD Desk and the doctor see why the patient is urgent.
    """
    from patients.models import (
        InitialAssessment, NurseQueueTicket, NurseTask, OPDAuditEvent,
    )

    designation = _designation_of(request)
    if not request.user.is_superuser and designation not in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.DOCTOR,
        UserProfile.Designation.ADMIN,
    ):
        return JsonResponse({"ok": False, "error": "OPD triage access required."}, status=403)

    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    ticket = None
    ticket_id = payload.get("ticket_id")
    if ticket_id:
        ticket = NurseQueueTicket.objects.select_related("patient").filter(pk=ticket_id).first()
        if not ticket:
            return JsonResponse({"ok": False, "error": "Queue ticket not found"}, status=404)
    patient = ticket.patient if ticket else Patient.objects.filter(pk=payload.get("patient_id")).first()
    if not patient:
        return JsonResponse({"ok": False, "error": "Patient not found"}, status=404)

    action = payload.get("action", "draft")
    if action not in ("draft", "complete"):
        return JsonResponse({"ok": False, "error": "Choose draft or complete."}, status=400)

    vitals = payload.get("vitals") if isinstance(payload.get("vitals"), dict) else {}

    def _value(key):
        return payload.get(key, vitals.get(key))

    def _num(v, cast):
        if v in (None, ""):
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    numeric_fields = {
        "temperature": (float, 25, 45),
        "pulse": (int, 1, 300),
        "respiratory_rate": (int, 1, 100),
        "bp_systolic": (int, 1, 350),
        "bp_diastolic": (int, 1, 250),
        "spo2": (int, 1, 100),
        "height": (float, 20, 250),
        "weight": (float, 0.2, 500),
        "bmi": (float, 1, 150),
        "rbs": (int, 1, 2000),
        "pain_score": (int, 0, 10),
    }
    values = {}
    for field, (cast, lower, upper) in numeric_fields.items():
        raw = _value(field)
        values[field] = _num(raw, cast)
        if raw not in (None, "") and values[field] is None:
            return JsonResponse(
                {"ok": False, "error": f"{field.replace('_', ' ').title()} must be numeric."},
                status=400,
            )
        if values[field] is not None and not lower <= values[field] <= upper:
            return JsonResponse(
                {"ok": False, "error": f"{field.replace('_', ' ').title()} is outside {lower}-{upper}."},
                status=400,
            )

    complaints = payload.get("complaints") or []
    has_complaint = (
        bool(complaints.strip()) if isinstance(complaints, str)
        else any(
            str(item.get("text", "") if isinstance(item, dict) else item).strip()
            for item in complaints
        )
    )
    if action == "complete" and not has_complaint:
        return JsonResponse(
            {"ok": False, "error": "Record the chief complaint before sending to the OPD Desk."},
            status=400,
        )
    # Safety: never hand a visit back without vitals.
    if action == "complete" and all(values[f] is None for f in ("pulse", "bp_systolic", "spo2", "temperature")):
        return JsonResponse(
            {"ok": False, "error": "At least one core vital (pulse, BP, SpO₂ or temperature) is required."},
            status=400,
        )

    assessment, _ = InitialAssessment.objects.get_or_create(
        patient=patient, defaults={"status": InitialAssessment.Status.DRAFT},
    )
    for field, value in values.items():
        setattr(assessment, field, value)
    if values["bmi"] is None and values["height"] and values["weight"]:
        assessment.bmi = round(values["weight"] / ((values["height"] / 100) ** 2), 1) or None

    if isinstance(complaints, str):
        assessment.complaints = complaints.strip()
    else:
        lines = []
        for item in complaints:
            text = (item.get("text", "") if isinstance(item, dict) else str(item)).strip()
            duration = (item.get("duration", "") if isinstance(item, dict) else "").strip()
            if text:
                lines.append(f"{text} ({duration})" if duration else text)
        assessment.complaints = "\n".join(lines)

    assessment.hpi = payload.get("hpi", "") or ""
    assessment.summary_note = payload.get("summary_note", "") or ""
    assessment.surgeries = payload.get("surgeries", "") or ""
    assessment.chronic_conditions = _normalise_conditions(payload.get("chronic_conditions"))
    assessment.allergies = [str(a).strip() for a in (payload.get("allergies") or []) if str(a).strip()]
    assessment.medications = _normalise_medications(payload.get("medications"))
    assessment.social_history = _normalise_social_history(payload.get("social_history"))
    examination = dict(payload.get("examination") or {})
    red_flags = payload.get("red_flags") or {}
    examination["red_flags"] = {
        key: bool(red_flags.get(key)) for key, _, _ in NURSE_RED_FLAGS
    }
    examination["nurse_observations"] = {
        "mobility": str(payload.get("mobility", "") or "")[:60],
        "consciousness": str(payload.get("consciousness", "") or "")[:60],
        "oxygen": str(payload.get("oxygen_support", "") or "")[:60],
        "observations": str(payload.get("observations", "") or "")[:2000],
    }
    assessment.examination = examination
    assessment.orders = _normalise_orders(payload.get("orders"))
    assessment.status = (
        InitialAssessment.Status.COMPLETED if action == "complete" else InitialAssessment.Status.DRAFT
    )
    if action == "complete":
        assessment.completed_by = request.user
        assessment.completed_at = timezone.now()
    assessment.save()

    score = _triage_priority_score(assessment)
    band = _priority_band(score)
    alerts = _nurse_vitals_alerts(assessment)
    escalated = bool(payload.get("escalate")) or band["label"] == "Emergency" or bool(alerts[:1] and band["label"] == "Emergency")

    if patient.allergies == "" and assessment.allergies:
        patient.allergies = ", ".join(assessment.allergies)
    if escalated and not patient.is_emergency:
        patient.is_emergency = True
    patient.save(update_fields=["allergies", "is_emergency", "updated_at"])

    if ticket is None:
        active_appointment = (
            Appointment.objects.filter(
                patient=patient,
                date__gte=timezone.localdate(),
            )
            .filter(status=Appointment.Status.SCHEDULED)
            .order_by("date", "start_time")
            .first()
        )
        ticket = NurseQueueTicket.objects.create(
            patient=patient,
            appointment=active_appointment,
            priority=_default_priority_for(patient),
            source=NurseQueueTicket.Source.REGISTRATION,
            sent_by=request.user,
        )

    if action == "complete":
        ticket.status = NurseQueueTicket.Status.COMPLETED
        ticket.completed_by = request.user
        ticket.completed_at = timezone.now()
        ticket.returned_at = timezone.now()
        override = payload.get("priority_override")
        if override in NurseQueueTicket.Priority.values:
            ticket.priority = override
        else:
            ticket.priority = {
                "Emergency": NurseQueueTicket.Priority.EMERGENCY,
                "Semi-urgent": NurseQueueTicket.Priority.URGENT,
            }.get(band["label"], ticket.priority)
        if ticket.priority == NurseQueueTicket.Priority.ROUTINE and escalated:
            ticket.priority = NurseQueueTicket.Priority.URGENT
    else:
        if ticket.is_open:
            ticket.status = NurseQueueTicket.Status.IN_ASSESSMENT
            ticket.claimed_by = ticket.claimed_by or request.user
            ticket.claimed_at = ticket.claimed_at or timezone.now()
    ticket.assessment = assessment
    ticket.escalated = escalated
    ticket.response_notes = str(payload.get("observations", "") or "")[:1000]
    eta = _num(payload.get("eta_minutes"), int)
    if eta is not None and 0 <= eta <= 600:
        ticket.eta_minutes = eta
    ticket.save()

    OPDAuditEvent.objects.create(
        patient=patient,
        actor=request.user,
        action="nurse_assessment_completed" if action == "complete" else "nurse_assessment_draft",
        summary=(
            f"{ticket.ticket_number} assessed ({band['label']}, score {score}) "
            "and returned to the OPD Desk"
            if action == "complete"
            else f"{ticket.ticket_number} vitals draft saved"
        ),
        details={
            "ticket_id": ticket.pk,
            "priority_score": score,
            "band": band["label"],
            "alerts": alerts,
            "escalated": escalated,
        },
    )

    # Optional follow-up nursing task straight from triage.
    task_title = str(payload.get("follow_up_task", "") or "").strip()
    if action == "complete" and task_title:
        NurseTask.objects.create(
            patient=patient,
            kind=payload.get("follow_up_kind") if payload.get("follow_up_kind") in NurseTask.Kind.values else NurseTask.Kind.OTHER,
            title=task_title[:255],
            priority=(
                NurseTask.Priority.URGENT if escalated else NurseTask.Priority.ROUTINE
            ),
            source=NurseTask.Source.TRIAGE,
            ticket=ticket,
            assigned_to=request.user,
            created_by=request.user,
        )

    if action == "complete":
        if _designation_of(request) == UserProfile.Designation.DOCTOR:
            # Doctors land on their own queue; the OPD pool entry is
            # assistants-only territory.
            handoff = reverse("patient_queue")
        else:
            handoff = f"/opd-assistant/patient-queue/?patient={patient.pk}"
    else:
        handoff = None

    return JsonResponse({
        "ok": True,
        "ticket_id": ticket.pk,
        "status": ticket.status,
        "priority_score": score,
        "band": band,
        "alerts": alerts,
        "escalated": escalated,
        "routed_to": "opd_desk" if action == "complete" else "nurse_queue",
        # Send the assistant straight to the patient's pool entry so the
        # doctor allocation form is one click away.
        "redirect": handoff,
    })


@login_required(login_url="login")
def nurse_patient_search(request):
    """Live patient search for the nurse desk (JSON)."""
    from django.db.models import Q

    denied = _require_nurse(request)
    if denied:
        return JsonResponse({"ok": False, "error": "OPD triage access required."}, status=403)

    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(uhid__icontains=query)
            | Q(phone__icontains=query)
        )
    results = []
    for p in patients.order_by("-created_at")[:20]:
        ticket = _nurse_ticket_for_patient(p)
        initials = "".join(part[0] for part in (p.first_name, p.last_name) if part)[:2].upper() or "PT"
        results.append({
            "id": p.pk,
            "name": p.full_name or "—",
            "initials": initials,
            "uhid": p.uhid or "",
            "meta": " · ".join(filter(None, [
                p.uhid or "", f"{p.age} yrs" if p.age is not None else "",
                p.get_gender_display() if p.gender else "", p.phone or "",
            ])),
            "emergency": bool(p.is_emergency),
            "in_queue": bool(ticket),
            "ticket_status": ticket.get_status_display() if ticket else "",
            "url": f"/nurse/triage/?patient={p.pk}",
        })
    return JsonResponse({"ok": True, "results": results})


@login_required(login_url="login")
def nurse_tasks(request):
    """Nursing task board — create, start and complete nursing tasks."""
    from patients.models import NurseTask

    denied = _require_nurse(request)
    if denied:
        return denied

    if request.method == "POST":
        patient = Patient.objects.filter(pk=request.POST.get("patient_id", "")).first()
        if patient is None:
            query = request.POST.get("patient_query", "").strip()
            if query:
                patient = Patient.objects.filter(
                    Q(uhid__iexact=query) | Q(phone__iexact=query)
                ).order_by("-created_at").first()
        title = request.POST.get("title", "").strip()[:255]
        if not patient or not title:
            messages.error(request, "Choose a patient and enter the task.")
        else:
            from core.nursing import resolve_assignee

            kind = request.POST.get("kind", NurseTask.Kind.VITALS)
            priority = request.POST.get("priority", NurseTask.Priority.ROUTINE)
            assigned_raw = request.POST.get("assigned_to", None)
            if assigned_raw is None and request.POST.get("assign_me") == "on":
                assigned_raw = "me"
            due_raw = request.POST.get("due_at", "").strip()
            due_at = None
            if due_raw:
                try:
                    from django.utils.dateparse import parse_datetime
                    due_at = parse_datetime(due_raw)
                    if due_at and timezone.is_naive(due_at):
                        due_at = timezone.make_aware(due_at)
                except (ValueError, TypeError):
                    due_at = None
            task = NurseTask.objects.create(
                patient=patient,
                kind=kind if kind in NurseTask.Kind.values else NurseTask.Kind.OTHER,
                title=title,
                notes=request.POST.get("notes", "").strip(),
                priority=priority if priority in NurseTask.Priority.values else NurseTask.Priority.ROUTINE,
                due_at=due_at,
                source=NurseTask.Source.MANUAL,
                assigned_to=resolve_assignee(assigned_raw, request.user),
                created_by=request.user,
            )
            messages.success(request, f"Nursing task '{task.title}' created for {patient.full_name}.")
        return redirect("nurse_tasks")

    from core.nursing import task_assignees
    from django.db.models import Q

    status_filter = request.GET.get("status", "open")
    scope = request.GET.get("scope", "")
    tasks = NurseTask.objects.select_related("patient", "assigned_to", "created_by", "order")
    if status_filter == "open":
        tasks = tasks.filter(status__in=[NurseTask.Status.PENDING, NurseTask.Status.IN_PROGRESS])
    elif status_filter in NurseTask.Status.values:
        tasks = tasks.filter(status=status_filter)
    if scope == "mine":
        tasks = tasks.filter(assigned_to=request.user)
    elif scope == "pool":
        tasks = tasks.filter(assigned_to__isnull=True)
    today = timezone.localdate()
    queue_patients = Patient.objects.filter(
        Q(created_at__date=today)
        | Q(appointments__date=today)
        | Q(nurse_tickets__sent_at__date=today)
        | Q(nurse_tasks__status__in=[NurseTask.Status.PENDING, NurseTask.Status.IN_PROGRESS])
    ).distinct().order_by("first_name", "last_name", "pk")[:80]
    from django.core.paginator import Paginator
    from django.db.models import Case, When, Value, IntegerField, F
    tasks = tasks.annotate(
        overdue_order=Case(
            When(status__in=[NurseTask.Status.PENDING, NurseTask.Status.IN_PROGRESS],
                 due_at__lt=timezone.now(), then=Value(0)),
            default=Value(1), output_field=IntegerField(),
        )
    ).order_by("overdue_order", F("due_at").asc(nulls_last=True), "created_at", "pk")
    tasks = Paginator(tasks, 50).get_page(request.GET.get("page"))

    return render(request, "core/nurse_tasks.html", {
        "tasks": tasks,
        "status_filter": status_filter,
        "kinds": NurseTask.Kind.choices,
        "priorities": NurseTask.Priority.choices,
        "task_presets": NURSE_TASK_PRESETS,
        "patients": queue_patients,
        "assignees": task_assignees(),
        "scope": scope,
        "counts": {
            "open": NurseTask.objects.filter(
                status__in=[NurseTask.Status.PENDING, NurseTask.Status.IN_PROGRESS]
            ).count(),
            "done_today": NurseTask.objects.filter(
                status=NurseTask.Status.DONE, completed_at__date=timezone.localdate()
            ).count(),
        },
    })


@login_required(login_url="login")
@require_POST
def nurse_task_update(request, task_id):
    """Move a nursing task through Pending → In progress → Done."""
    from patients.models import NurseTask, OPDAuditEvent

    task = get_object_or_404(
        NurseTask.objects.select_related("order__department", "patient"), pk=task_id
    )
    if not _can_work_nurse_task(request, task):
        denied = _require_nurse(request)
        return denied or redirect("home")
    next_status = request.POST.get("status", "") or task.status
    if next_status not in NurseTask.Status.values:
        messages.error(request, "Choose a valid task status.")
        return redirect("nurse_tasks")

    from core.nursing import resolve_assignee, sync_order_from_task

    task.status = next_status
    if "assigned_to" in request.POST:
        task.assigned_to = resolve_assignee(request.POST.get("assigned_to"), request.user)
    if next_status == NurseTask.Status.IN_PROGRESS and not task.assigned_to:
        task.assigned_to = request.user
    if next_status == NurseTask.Status.DONE:
        task.completed_by = request.user
        task.completed_at = timezone.now()
        if not task.assigned_to_id:
            task.assigned_to = request.user
    task.outcome = request.POST.get("outcome", "").strip()[:255] or task.outcome
    task.save()
    if next_status == NurseTask.Status.DONE:
        sync_order_from_task(task, request.user)
    OPDAuditEvent.objects.create(
        patient=task.patient,
        actor=request.user,
        action="nurse_task_updated",
        summary=f"{task.title} → {task.get_status_display()}",
        details={"task_id": task.pk, "status": next_status},
    )
    messages.success(request, f"{task.title} marked {task.get_status_display().lower()}.")
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("nurse_tasks")


@login_required(login_url="login")
def nurse_handover(request):
    """Shift handover sheet: open queue, in-progress care and completed rounds."""
    from patients.models import InitialAssessment, NurseQueueTicket, NurseTask

    denied = _require_nurse(request)
    if denied:
        return denied

    today = timezone.localdate()
    tickets = list(
        NurseQueueTicket.objects.filter(sent_at__date=today)
        .select_related("patient", "claimed_by", "completed_by", "sent_by")
    )
    tickets.sort(key=lambda t: (_nurse_priority_token(t), t.sent_at))
    assessed = [t for t in tickets if t.status == NurseQueueTicket.Status.COMPLETED]
    pending = [t for t in tickets if t.status in NURSE_OPEN_STATUSES]
    pending.sort(key=_nurse_priority_token)

    rows = []
    for t in assessed:
        a = t.assessment
        rows.append({
            "ticket": t,
            "patient": t.patient,
            "score": _triage_priority_score(a) if a else 0,
            "alerts": _nurse_vitals_alerts(a),
            "assessment": a,
            "nurse": t.completed_by,
        })

    open_tasks = list(NurseTask.objects.filter(
        status__in=[NurseTask.Status.PENDING, NurseTask.Status.IN_PROGRESS]
    ).select_related("patient", "assigned_to"))
    open_tasks.sort(key=lambda t: (0 if t.is_overdue else 1, t.due_at or timezone.now()))
    overdue_tasks = [task for task in open_tasks if task.is_overdue]
    carried_tasks = [
        task for task in open_tasks
        if not task.is_overdue and task.created_at.date() < today
        and (task.due_at is None or timezone.localtime(task.due_at).date() < today)
    ]
    shift_tasks = [
        task for task in open_tasks
        if task not in overdue_tasks and task not in carried_tasks
    ]

    stats = {
        "received": len(tickets),
        "assessed": len(assessed),
        "pending": len(pending),
        "emergency": sum(
            1 for t in tickets
            if t.priority == NurseQueueTicket.Priority.EMERGENCY
        ),
        "avg_wait": (
            round(sum(t.wait_minutes for t in assessed) / len(assessed)) if assessed else 0
        ),
        "escalated": sum(1 for t in tickets if t.escalated),
    }

    return render(request, "core/nurse_handover.html", {
        "today": today,
        "rows": rows,
        "pending": pending,
        "open_tasks": open_tasks,
        "overdue_tasks": overdue_tasks,
        "shift_tasks": shift_tasks,
        "carried_tasks": carried_tasks,
        "stats": stats,
        "assessments_today": InitialAssessment.objects.filter(
            completed_at__date=today
        ).select_related("patient", "completed_by").order_by("-completed_at")[:30],
    })
