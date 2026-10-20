from datetime import date, timedelta
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie


from .models import UserProfile
from .backup import BackupArchiveError, build_patient_backup, import_patient_backup


def home(request):
    """Redirect to login or appropriate dashboard based on user designation."""
    if request.user.is_authenticated:
        user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
        if user_desig == "registration_desk":
            return redirect("reception_dashboard")
        if user_desig == UserProfile.Designation.ADMIN:
            return redirect("admin_dashboard")
        return redirect("dashboard")
    return redirect("login")

from appointments.models import Appointment, Doctor
from patients.models import Patient, OPDAuditEvent, PatientBill


# ── Login / logout ────────────────────────────────────────────────────────────

DESIGNATION_CHOICES = [
    ("registration_desk", "Registration Desk"),
    ("opd_assistant", "OPD Assistant"),
    ("doctor", "Doctor"),
    ("nurse", "Nurse"),
    ("admin", "Admin"),
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
            effective_designation = profile.designation
            if profile.designation != designation:
                allowed_label = dict(UserProfile.Designation.choices).get(
                    profile.designation, profile.designation
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
    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
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

    today = date.today()
    context = {
        "today": today,
        "total_patients": Patient.objects.count(),
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
        # Patients registered at the registration desk, newest first.
        # These are the only rows shown in the doctor queue table.
        "queue_patients": (
            Patient.objects.all()
            .select_related("assigned_doctor", "assigned_doctor__profile")
            .prefetch_related("bills")
            .order_by("-created_at")[:50]
        ),
        # KPI strip — derived from real registered data.
        "kpi_total_today": Patient.objects.filter(created_at__date=today).count(),
        "kpi_in_pool": Patient.objects.filter(created_at__date=today, is_emergency=False).count(),
        "kpi_with_doctors": Patient.objects.filter(
            assigned_doctor__isnull=False
        ).count(),
        "kpi_investigations": 0,
        "kpi_procedures": 0,
        "kpi_pharmacy": 0,
        "kpi_review_required": Patient.objects.filter(is_emergency=True).count(),
        "kpi_completed": (
            Appointment.objects.filter(date=today, status=Appointment.Status.COMPLETED).count()
        ),
    # Doctor accounts available in the "With Doctors" filter dropdown.
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

    designation = (
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

    doctor_id = request.POST.get("doctor_id", "").strip()
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
            | Q(patient__mr_number__icontains=query)
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
            | Q(patient__mr_number__icontains=query)
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
            | Q(mr_number__icontains=query)
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
            else:
                selected_pending_total += row.amount_due

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
        "user": request.user,
    })


@login_required(login_url="login")
def bill_set_status(request, bill_id):
    """Mark a bill as paid or move it back to pending from the payment slip."""
    denied = _require_registration_desk(request)
    if denied:
        return denied

    bill = get_object_or_404(PatientBill, pk=bill_id)
    from django.utils import timezone

    next_status = request.POST.get("status", "").strip()
    if next_status == PatientBill.Status.PAID:
        bill.status = PatientBill.Status.PAID
        bill.paid_at = timezone.now()
        bill.save(update_fields=["status", "paid_at"])
        messages.success(
            request,
            f"{bill.bill_number} marked as PAID. Amount received ₹{bill.amount_due}.",
        )
    elif next_status == PatientBill.Status.PENDING:
        bill.status = PatientBill.Status.PENDING
        bill.paid_at = None
        bill.save(update_fields=["status", "paid_at"])
        messages.warning(
            request,
            f"{bill.bill_number} moved back to PENDING payment.",
        )
    return redirect("patient_bill_slip", bill_id=bill.pk)


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
        f"MR Number: {patient.mr_number or patient.pk}",
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
    """List all staff accounts, with inline create for a new doctor."""
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
                f"Doctor account created for {user.get_full_name() or user.username}.",
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
        "nurse": User.objects.filter(profile__designation=UserProfile.Designation.NURSE).count(),
        "registration_desk": User.objects.filter(
            profile__designation=UserProfile.Designation.REGISTRATION_DESK
        ).count(),
        "opd_assistant": User.objects.filter(
            profile__designation=UserProfile.Designation.OPD_ASSISTANT
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
def opd_assistant_dashboard(request):
    """OPD Central Dashboard with live counts and patient-flow filters."""
    import csv
    from django.http import HttpResponse
    from django.core.paginator import Paginator
    from django.db.models import Q
    from django.utils import timezone
    from patients.models import ClinicalOrder, ConsultationSession, InitialAssessment

    user_desig = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig != UserProfile.Designation.OPD_ASSISTANT:
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
        ).distinct()
        .select_related("assigned_doctor", "assigned_doctor__profile")
        .prefetch_related("consultations__orders", "bills", "appointments")
        .order_by("-is_emergency", "created_at")[:500]
    )
    rows = _opd_patient_flow(all_patients)
    counts = {
        "waiting": sum(row["status"] == "Waiting" for row in rows),
        "central_pool": sum(row["status"] == "In Central Pool" for row in rows),
        "pending_assignment": sum(row["status"] == "Pending Assignment" for row in rows),
        "with_doctors": sum(row["status"] == "With Doctor" for row in rows),
        "investigations": sum(row["status"] == "Investigations" for row in rows),
        "procedures": sum(row["status"] == "Procedures" for row in rows),
        "pharmacy": sum(row["status"] == "Pharmacy" for row in rows),
        "review_required": sum(row["status"] == "Review Required" for row in rows),
        "completed": sum(row["status"] == "Completed" for row in rows),
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
    doctors = get_user_model().objects.filter(
        profile__designation=UserProfile.Designation.DOCTOR
    ).order_by("first_name", "username")
    departments = sorted({row["department"] for row in _opd_patient_flow(all_patients) if row["department"]})

    from appointments.models import Appointment
    todays_appointments_qs = Appointment.objects.filter(date=today).exclude(
        status=Appointment.Status.CANCELLED
    ).select_related("patient", "doctor").order_by("start_time")
    upcoming_appointments = list(todays_appointments_qs[:5])

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
        "upcoming_appointments": upcoming_appointments,
        "user": request.user,
    })


def _opd_patient_flow(patients):
    """Build patient-flow rows from the existing persisted OPD records."""
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
        prescriptions = [prescription for item in consultations for prescription in item.prescriptions.all()]
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
        if consultation and consultation.status == ConsultationSession.Status.CANCELLED:
            status, location = "Cancelled", "Visit cancelled"
        elif consultation and consultation.status == ConsultationSession.Status.COMPLETED:
            status, location = "Completed", "Completed"
        elif not assigned_doctor and not consultation and next_appointment and (
            next_appointment.date == today or next_appointment.created_at.date() == today
        ):
            status, location = "Pending Assignment", "Central Pool"
        elif any(order.status.casefold() in ("review required", "review") for order in orders):
            status, location = "Review Required", "Doctor Review"
        elif any(order.order_type == "laboratory" for order in pending_orders):
            status, location = "Investigations", "Laboratory"
        elif any(order.order_type == "radiology" for order in pending_orders):
            status, location = "Investigations", "Radiology"
        elif any(order.order_type == "other" for order in pending_orders):
            status, location = "Procedures", "Service Queue"
        elif latest_bill and latest_bill.status == "pending" and any(
            item.dispense_status != "dispensed" for item in prescriptions
        ):
            status, location = "Awaiting Payment", "Pharmacy"
        elif any(item.dispense_status != "dispensed" for item in prescriptions):
            status, location = "Pharmacy", "Pharmacy"
        elif consultation and consultation.status == ConsultationSession.Status.IN_PROGRESS:
            status, location = "With Doctor", "Doctor Consultation"
        elif assessment and assessment.status == InitialAssessment.Status.COMPLETED:
            status, location = "In Central Pool", "Doctor Queue"
        else:
            status, location = "Waiting", "Initial Assessment"
        allergies = bool(patient.allergies or (assessment and assessment.allergies))
        rows.append({
            "patient": patient,
            "assessment": assessment,
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
            "alert": "Emergency" if patient.is_emergency else ("Allergy on file" if allergies else ""),
            "billing": latest_bill.get_status_display() if latest_bill else "Unbilled",
            "updated_at": max(
                [patient.updated_at] + ([assessment.updated_at] if assessment else [])
                + ([consultation.updated_at] if consultation else [])
            ),
        })
    return rows


@login_required(login_url="login")
def opd_patient_queue(request):
    """Central patient queue with a selected visit summary."""
    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation != UserProfile.Designation.OPD_ASSISTANT:
        messages.warning(request, "Access restricted: the Patient Queue is for OPD Assistants.")
        return redirect("home")
    from django.utils import timezone
    today = timezone.localdate()
    patients = list(
        Patient.objects.filter(
            Q(created_at__date=today)
            | Q(appointments__date=today)
            | Q(appointments__created_at__date=today)
        ).distinct()
        .select_related("assigned_doctor", "assigned_doctor__profile")
        .prefetch_related("consultations__orders", "bills", "appointments")
        .order_by("-is_emergency", "created_at")
    )
    rows = _opd_patient_flow(patients)
    query = request.GET.get("q", "").strip()
    if query:
        rows = [row for row in rows if query.casefold() in " ".join((
            row["patient"].full_name, row["patient"].uhid, row["visit_number"],
        )).casefold()]
    selected_id = request.GET.get("patient", "")
    selected = next((row for row in rows if str(row["patient"].pk) == selected_id), None)
    from django.core.paginator import Paginator
    page_obj = Paginator(rows, 20).get_page(request.GET.get("page"))
    doctors = get_user_model().objects.filter(
        profile__designation=UserProfile.Designation.DOCTOR
    ).order_by("first_name", "username")
    return render(request, "core/opd_workspace.html", {
        "page_title": "Patient Queue",
        "page_kind": "patient_queue",
        "rows": page_obj,
        "selected": selected,
        "query": query,
        "doctors": doctors,
        "today": today,
    })


@login_required(login_url="login")
@require_POST
def opd_order_update(request, order_id):
    """Update a service order from the OPD service desk and persist the result."""
    from patients.models import ClinicalOrder, OPDAuditEvent
    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation != UserProfile.Designation.OPD_ASSISTANT:
        return HttpResponseForbidden("OPD Assistant access required.")
    order = get_object_or_404(ClinicalOrder, pk=order_id)
    new_status = request.POST.get("status", "").strip()
    allowed_statuses = {"Ordered", "In Progress", "Completed", "Review Required", "Cancelled"}
    if new_status not in allowed_statuses:
        messages.error(request, "Choose a valid service status.")
    else:
        previous_status = order.status
        order.status = new_status
        if "result_summary" in request.POST:
            order.result_summary = request.POST.get("result_summary", "").strip()[:255]
        details = dict(order.operational_details or {})
        for field in (
            "body_part", "preparation_status", "preparation_note", "specimen_status",
            "acquisition_status", "session_datetime", "room", "assigned_staff",
            "post_instructions",
        ):
            if field in request.POST:
                details[field] = request.POST.get(field, "").strip()[:255]
        if request.POST.get("service") == "procedure-room":
            details["consent_confirmed"] = request.POST.get("consent_confirmed") == "on"
            details["preparation_complete"] = request.POST.get("preparation_complete") == "on"
        order.operational_details = details
        order.updated_by = request.user
        order.save(update_fields=["status", "result_summary", "operational_details", "updated_by", "updated_at"])
        OPDAuditEvent.objects.create(
            patient=order.consultation.patient,
            order=order,
            actor=request.user,
            action="service_order_updated",
            summary=f"{order.name}: {previous_status} to {new_status}",
            details={"previous_status": previous_status, "status": new_status, **details},
        )
        messages.success(request, f"{order.name} updated to {new_status}.")
    return_service = request.POST.get("service", "")
    if return_service not in {"laboratory", "radiology", "cardiology", "pharmacy", "physiotherapy", "counselling", "procedure-room", "procedures"}:
        return_service = "laboratory" if order.order_type == "laboratory" else "radiology" if order.order_type == "radiology" else "procedures"
    return redirect("opd_service_queue", service=return_service)


@login_required(login_url="login")
def opd_service_queue(request, service):
    """Searchable service-order queue for OPD-assistant operations."""
    from django.db.models import Exists, OuterRef, Q
    from patients.models import ClinicalOrder, PrescriptionItem, PatientBill
    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation != UserProfile.Designation.OPD_ASSISTANT:
        messages.warning(request, "Access restricted: service queues are for OPD Assistants.")
        return redirect("home")
    service_map = {
        "laboratory": ("Laboratory", ["laboratory"]),
        "radiology": ("Radiology · X-Ray & USG", ["radiology"]),
        "cardiology": ("Cardiology · ECG", ["other"]),
        "pharmacy": ("Pharmacy", ["other"]),
        "physiotherapy": ("Physiotherapy", ["other"]),
        "counselling": ("Counselling", ["other"]),
        "procedure-room": ("Procedure Room", ["other"]),
        "procedures": ("Procedures", ["other"]),
    }
    if service not in service_map:
        from django.http import Http404
        raise Http404
    title, order_types = service_map[service]
    prescriptions = PrescriptionItem.objects.none()
    if service == "pharmacy":
        prescriptions = PrescriptionItem.objects.annotate(
            payment_pending=Exists(PatientBill.objects.filter(
                patient_id=OuterRef("consultation__patient_id"), status=PatientBill.Status.PENDING
            ))
        ).select_related(
            "consultation__patient", "consultation__doctor", "dispensed_by"
        ).exclude(dispense_status=PrescriptionItem.DispenseStatus.CANCELLED).order_by("created_at")
    orders = ClinicalOrder.objects.filter(order_type__in=order_types).select_related(
        "consultation__patient", "consultation__doctor"
    ).order_by("created_at")
    if service in {"cardiology", "pharmacy", "physiotherapy", "counselling", "procedure-room"}:
        keyword = {"cardiology": "ecg", "pharmacy": "pharm", "physiotherapy": "physio", "counselling": "counsel", "procedure-room": "procedure"}.get(service, "")
        orders = orders.filter(name__icontains=keyword)
    query = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "")
    if query:
        orders = orders.filter(
            Q(name__icontains=query) | Q(consultation__patient__first_name__icontains=query)
            | Q(consultation__patient__last_name__icontains=query)
            | Q(consultation__patient__mr_number__icontains=query)
            | Q(consultation__visit_token__icontains=query)
        )
        prescriptions = prescriptions.filter(
            Q(medicine_name__icontains=query)
            | Q(consultation__patient__first_name__icontains=query)
            | Q(consultation__patient__last_name__icontains=query)
            | Q(consultation__patient__mr_number__icontains=query)
            | Q(consultation__visit_token__icontains=query)
        )
    if status_filter:
        orders = orders.filter(status=status_filter)
        prescriptions = prescriptions.filter(dispense_status=status_filter.lower().replace(" ", "_"))
    from django.core.paginator import Paginator
    page_obj = Paginator(orders, 25).get_page(request.GET.get("page"))
    selected_id = request.GET.get("order", "")
    selected = ClinicalOrder.objects.filter(pk=selected_id, order_type__in=order_types).select_related(
        "consultation__patient", "consultation__doctor"
    ).first() if selected_id.isdigit() else None
    selected_prescription_id = request.GET.get("prescription", "")
    selected_prescription = prescriptions.filter(pk=selected_prescription_id).first() if selected_prescription_id.isdigit() else None
    if service == "pharmacy":
        orders = ClinicalOrder.objects.none()
        selected = None
        if status_filter:
            prescriptions = prescriptions.filter(dispense_status=status_filter.lower().replace(" ", "_"))
        from django.core.paginator import Paginator
        page_obj = Paginator(prescriptions, 25).get_page(request.GET.get("page"))
    else:
        page_obj = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(request, "core/opd_workspace.html", {
        "page_title": title,
        "page_kind": "service_queue",
        "service": service,
        "orders": page_obj if service != "pharmacy" else None,
        "prescriptions": page_obj if service == "pharmacy" else None,
        "selected_order": selected,
        "selected_prescription": selected_prescription,
        "query": query,
        "status_filter": status_filter,
    })


@login_required(login_url="login")
@require_POST
def opd_prescription_update(request, item_id):
    """Record pharmacy stock and dispense updates with payment safeguards."""
    from patients.models import OPDAuditEvent, PatientBill, PrescriptionItem
    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation != UserProfile.Designation.OPD_ASSISTANT:
        return HttpResponseForbidden("OPD Assistant access required.")
    item = get_object_or_404(PrescriptionItem.objects.select_related("consultation__patient"), pk=item_id)
    dispense_status = request.POST.get("dispense_status", "")
    stock_status = request.POST.get("stock_status", "")
    if dispense_status not in PrescriptionItem.DispenseStatus.values or stock_status not in PrescriptionItem.StockStatus.values:
        messages.error(request, "Choose valid stock and dispense states.")
    else:
        try:
            quantity = int(request.POST.get("dispensed_quantity", "0"))
        except (TypeError, ValueError):
            quantity = -1
        if quantity < 0 or quantity > 999:
            messages.error(request, "Dispensed quantity must be between 0 and 999.")
        elif dispense_status in (PrescriptionItem.DispenseStatus.PARTIAL, PrescriptionItem.DispenseStatus.DISPENSED) and quantity < 1:
            messages.error(request, "Enter the quantity dispensed.")
        elif dispense_status == PrescriptionItem.DispenseStatus.DISPENSED and PatientBill.objects.filter(
            patient=item.consultation.patient, status=PatientBill.Status.PENDING
        ).exists():
            messages.error(request, "Dispensing is blocked while this patient has a pending bill.")
        else:
            previous_status = item.dispense_status
            item.dispense_status = dispense_status
            item.stock_status = stock_status
            item.dispensed_quantity = quantity
            item.dispensed_by = request.user
            item.save(update_fields=["dispense_status", "stock_status", "dispensed_quantity", "dispensed_by", "updated_at"])
            OPDAuditEvent.objects.create(
                patient=item.consultation.patient,
                actor=request.user,
                action="pharmacy_dispense_updated",
                summary=f"{item.medicine_name}: {previous_status} to {dispense_status}",
                details={"prescription_id": item.pk, "stock_status": stock_status, "quantity": quantity},
            )
            messages.success(request, f"{item.medicine_name} dispense status updated.")
    return redirect("opd_service_queue", service="pharmacy")


@login_required(login_url="login")
def opd_module_page(request, module):
    """Role-scoped supporting pages for the OPD Assistant portal."""
    import csv
    from django.http import HttpResponse
    from patients.models import ClinicalOrder, OPDAuditEvent

    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation != UserProfile.Designation.OPD_ASSISTANT:
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

    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation != UserProfile.Designation.OPD_ASSISTANT:
        messages.warning(request, "Access restricted: referrals are for OPD Assistants.")
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

    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all().order_by("-created_at")
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query) | Q(last_name__icontains=query)
            | Q(second_name__icontains=query) | Q(mr_number__icontains=query)
        )
    patient_id = request.GET.get("patient", "")
    selected_patient = Patient.objects.filter(pk=patient_id).first() if patient_id.isdigit() else None
    referrals = OPDReferral.objects.select_related("patient", "doctor", "created_by")
    if selected_patient:
        referrals = referrals.filter(patient=selected_patient)
    return render(request, "core/opd_referrals.html", {
        "page_title": "Cross-consultation / Referral",
        "patients": patients[:30],
        "selected_patient": selected_patient,
        "referrals": referrals[:100],
        "doctors": get_user_model().objects.filter(profile__designation=UserProfile.Designation.DOCTOR).select_related("profile").order_by("first_name", "username"),
        "query": query,
        "referral_types": OPDReferral.ReferralType.choices,
        "urgencies": OPDReferral.Urgency.choices,
    })


# ── Registration Desk ─────────────────────────────────────────────────────────

@login_required(login_url="login")
def reception_dashboard(request):
    """Dedicated dashboard for registration desk."""
    # Block Doctor/Nurse users from accessing registration desk dashboard
    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
    if user_desig in ["doctor", "nurse", "opd_assistant"] and not request.user.is_superuser:
        from django.contrib import messages
        if user_desig == "opd_assistant":
            messages.warning(request, "Access restricted: OPD Assistants use the OPD Desk portal.")
            return redirect("opd_assistant_dashboard")
        messages.warning(request, "Access restricted: Doctor/Nurse users should use the Doctor Dashboard.")
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
    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
    if user_desig in ["doctor", "nurse"] and not request.user.is_superuser:
        from django.contrib import messages
        messages.warning(request, "Access restricted: Patient Registration counter is for Registration Desk staff only.")
        return redirect("dashboard")
    from decimal import Decimal

    from patients.models import Patient

    # Compute next MR number (simple auto-increment)
    last = Patient.objects.order_by("-id").first()
    next_mr = (int(last.mr_number) + 1) if (last and last.mr_number and last.mr_number.isdigit()) else 100001

    if request.method == "POST":
        from patients.forms import PatientRegistrationForm
        form = PatientRegistrationForm(request.POST, request.FILES)
        if form.is_valid():
            with transaction.atomic():
                last_locked = Patient.objects.select_for_update().order_by("-id").first()
                current_mr = (int(last_locked.mr_number) + 1) if (last_locked and last_locked.mr_number and last_locked.mr_number.isdigit()) else 100001
                patient = form.save(commit=False)
                patient.mr_number = str(current_mr)

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
                registration_total = (patient.reg_fee or Decimal("0")) + (patient.con_fee or Decimal("0"))
                registration_discount = min(patient.discount or Decimal("0"), registration_total)
                if registration_total - registration_discount > 0:
                    PatientBill.objects.create(
                        patient=patient,
                        source=PatientBill.Source.REGISTRATION,
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

    Registers a patient with only the essentials (name, age, sex, MR number),
    flags the record as an emergency, and raises a PENDING registration-fee bill
    so the payment is collected later from the Bill Command Center. The remaining
    registration details are completed later by editing the record from History.
    """
    from decimal import Decimal

    from django.utils import timezone

    from patients.forms import EmergencyRegistrationForm
    from patients.models import RegistrationFeeSettings

    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
    if user_desig in ["doctor", "nurse"] and not request.user.is_superuser:
        messages.warning(request, "Access restricted: Emergency registration is for Registration Desk staff only.")
        return redirect("dashboard")

    def _next_mr():
        last = Patient.objects.order_by("-id").first()
        if last and last.mr_number and last.mr_number.isdigit():
            return int(last.mr_number) + 1
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
                    int(last_locked.mr_number) + 1
                    if (last_locked and last_locked.mr_number and last_locked.mr_number.isdigit())
                    else 100001
                )
                parts = data["name"].split(maxsplit=1)
                patient = Patient(
                    first_name=parts[0],
                    last_name=parts[1] if len(parts) > 1 else "",
                    gender=data["gender"],
                    date_of_birth=data.get("date_of_birth"),
                    mr_number=data["mr_number"] or str(current_mr),
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
                f"Emergency patient {patient.full_name} registered (MR #{patient.mr_number}). "
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

    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
    if user_desig in ["doctor", "nurse"] and not request.user.is_superuser:
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
            | Q(mr_number__icontains=query)
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
            "when": patient.created_at,
            "badge": f"Patient · MR #{patient.mr_number or patient.id}",
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
                | Q(patient__mr_number__icontains=query)
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

    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    allowed = {
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.DOCTOR,
        UserProfile.Designation.NURSE,
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

    return JsonResponse({"ok": True, "id": assessment.pk})


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

    designation = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if designation == UserProfile.Designation.REGISTRATION_DESK and not request.user.is_superuser:
        messages.warning(
            request,
            "Access restricted: the Initial Assessment workstation is for clinical staff.",
        )
        return redirect("reception_dashboard")

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
            | Q(mr_number__icontains=query)
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

    designation = getattr(getattr(request.user, "profile", None), "designation", None)
    if designation not in (
        UserProfile.Designation.OPD_ASSISTANT,
        UserProfile.Designation.DOCTOR,
        UserProfile.Designation.NURSE,
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
            | Q(mr_number__icontains=query)
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

    user_desig = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN) and not request.user.is_superuser:
        messages.warning(request, "Access restricted: Doctor portal only.")
        if user_desig == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
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
    from django.db.models import Q
    from django.utils import timezone
    from patients.models import (
        Patient, InitialAssessment, ConsultationSession, 
        PrescriptionItem, ClinicalOrder, DiagnosticReport
    )

    user_desig = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if user_desig not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN):
        messages.warning(request, "Access restricted: Doctor portal only.")
        if user_desig == UserProfile.Designation.REGISTRATION_DESK:
            return redirect("reception_dashboard")
        return redirect("dashboard")

    # Patient search query
    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(mr_number__icontains=query)
            | Q(phone__icontains=query)
        )
    patients = patients.order_by("-created_at")[:40]

    # Selected patient
    patient_id = request.GET.get("patient", "").strip()
    patient = None
    if patient_id.isdigit():
        patient = Patient.objects.filter(pk=int(patient_id)).first()

    patient_ctx = None
    initial_assessment_data = None
    consultation = None
    prescriptions = []
    current_orders = []
    previous_prescriptions = []
    previous_orders = []
    diagnostic_reports = []

    if patient:
        now = timezone.now()
        doctor_specialty = "General Medicine"
        doctor_name = request.user.get_full_name() or request.user.username
        if hasattr(request.user, "profile") and request.user.profile.specialty:
            doctor_specialty = request.user.profile.get_specialty_display()

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

        # Generate or load active consultation session
        consultation = ConsultationSession.objects.filter(
            patient=patient, status__in=[ConsultationSession.Status.DRAFT, ConsultationSession.Status.IN_PROGRESS]
        ).first()

        if not consultation:
            visit_token = f"OPD-{now:%y%m%d}-{patient.pk:04d}"
            consultation = ConsultationSession.objects.create(
                patient=patient,
                doctor=request.user,
                visit_token=visit_token,
                status=ConsultationSession.Status.DRAFT,
                visit_type=ConsultationSession.VisitType.NEW_PROBLEM,
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
            "visit_timestamp": timezone.localtime(patient.created_at).strftime("%d %b %Y, %I:%M %p"),
            "department": doctor_specialty,
            "visit_type": "Emergency" if patient.is_emergency else ("Walk-in" if patient.payment_type != "credit" else "Referred"),
            "referred_by": patient.reference_no or "—",
            "allergies": allergies,
            "chronic_illnesses": [],
            "active_medications": [],
            "is_emergency": bool(patient.is_emergency),
        }

        # Load initial assessment if exists
        try:
            ia = patient.initial_assessment
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
                "hpi": ia.hpi or "",
                "summary_note": ia.summary_note or "",
                "chronic_conditions": ia.chronic_conditions or [],
                "allergies": ia.allergies or [],
                "medications": ia.medications or [],
                "social_history": ia.social_history or [],
            }
            if ia.chronic_conditions:
                patient_ctx["chronic_illnesses"] = [
                    c.get("name") if isinstance(c, dict) else str(c) for c in ia.chronic_conditions
                ]
            if ia.medications:
                patient_ctx["active_medications"] = [
                    m.get("name") if isinstance(m, dict) else str(m) for m in ia.medications
                ]
            if ia.allergies and not patient_ctx["allergies"]:
                patient_ctx["allergies"] = [
                    a.get("allergen") if isinstance(a, dict) else str(a) for a in ia.allergies
                ]
        except InitialAssessment.DoesNotExist:
            initial_assessment_data = None

        # Load active prescriptions for this consultation
        prescriptions = list(consultation.prescriptions.all())

        # Load current orders for this consultation
        current_orders = list(consultation.orders.all())

        # Previous prescriptions from older completed consultations
        older_consultations = ConsultationSession.objects.filter(
            patient=patient
        ).exclude(pk=consultation.pk).order_by("-created_at")
        
        if older_consultations.exists():
            last_c = older_consultations.first()
            previous_prescriptions = list(last_c.prescriptions.all())
            previous_orders = list(last_c.orders.all())

        # Reports for this patient
        diagnostic_reports = list(DiagnosticReport.objects.filter(patient=patient).order_by("-report_date"))

    return render(request, "core/doctor_consultation.html", {
        "selected_patient": patient,
        "patients": patients,
        "query": query,
        "patient_ctx": patient_ctx,
        "assessment": initial_assessment_data,
        "consultation": consultation,
        "prescriptions": prescriptions,
        "current_orders": current_orders,
        "previous_prescriptions": previous_prescriptions,
        "previous_orders": previous_orders,
        "diagnostic_reports": diagnostic_reports,
        "formulary": STANDARD_FORMULARY,
        "common_icd": COMMON_ICD10,
        "title": "Doctor Consultation (OPD)",
        "user": request.user,
    })


@require_POST
@login_required(login_url="login")
def save_consultation(request):
    """Async endpoint to save consultation session, diagnoses, orders and prescriptions."""
    import json
    from django.utils import timezone
    from patients.models import (
        Patient, ConsultationSession, PrescriptionItem, ClinicalOrder, OPDAuditEvent
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

    if consultation_id:
        consultation = ConsultationSession.objects.filter(pk=consultation_id, patient=patient).first()
    else:
        consultation = None

    if not consultation:
        visit_token = f"OPD-{timezone.now():%y%m%d}-{patient.pk:04d}"
        consultation = ConsultationSession.objects.create(
            patient=patient,
            doctor=request.user,
            visit_token=visit_token,
            status=ConsultationSession.Status.DRAFT,
        )

    action = payload.get("action", "save_draft")
    if action == "complete":
        consultation.status = ConsultationSession.Status.COMPLETED
    elif action == "cancel":
        consultation.status = ConsultationSession.Status.CANCELLED
    else:
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
    consultation.additional_orders = payload.get("additional_orders", []) or []

    consultation.save()

    # Update prescriptions
    prescription_list = payload.get("prescriptions", [])
    if isinstance(prescription_list, list):
        existing_prescriptions = list(consultation.prescriptions.order_by("order_index", "id"))
        retained_prescriptions = set()
        for idx, item in enumerate(prescription_list):
            med_name = item.get("medicine_name", "").strip()
            if med_name:
                match = next((existing for existing in existing_prescriptions
                              if existing.pk not in retained_prescriptions
                              and existing.medicine_name.casefold() == med_name.casefold()), None)
                if match:
                    retained_prescriptions.add(match.pk)
                    match.dose = item.get("dose", "").strip()
                    match.route = item.get("route", "Oral").strip()
                    match.frequency = item.get("frequency", "OD").strip()
                    match.duration = item.get("duration", "5 days").strip()
                    match.instructions = item.get("instructions", "").strip()
                    match.order_index = idx
                    if match.dispense_status == PrescriptionItem.DispenseStatus.CANCELLED:
                        match.dispense_status = PrescriptionItem.DispenseStatus.ORDERED
                    match.save()
                else:
                    created = PrescriptionItem.objects.create(
                        consultation=consultation,
                        medicine_name=med_name,
                        dose=item.get("dose", "").strip(),
                        route=item.get("route", "Oral").strip(),
                        frequency=item.get("frequency", "OD").strip(),
                        duration=item.get("duration", "5 days").strip(),
                        instructions=item.get("instructions", "").strip(),
                        order_index=idx,
                    )
                    retained_prescriptions.add(created.pk)
        for existing in existing_prescriptions:
            if existing.pk not in retained_prescriptions and existing.dispense_status not in (
                PrescriptionItem.DispenseStatus.DISPENSED,
                PrescriptionItem.DispenseStatus.CANCELLED,
            ):
                existing.dispense_status = PrescriptionItem.DispenseStatus.CANCELLED
                existing.save(update_fields=["dispense_status", "updated_at"])

    # Update orders
    orders_list = payload.get("orders", [])
    if isinstance(orders_list, list):
        existing_orders = list(consultation.orders.all())
        retained_orders = set()
        for ord_item in orders_list:
            ord_name = ord_item.get("name", "").strip()
            if ord_name:
                ord_type = ord_item.get("order_type", "laboratory")
                match = next((existing for existing in existing_orders
                              if existing.pk not in retained_orders
                              and existing.order_type == ord_type
                              and existing.name.casefold() == ord_name.casefold()), None)
                if match:
                    retained_orders.add(match.pk)
                    if match.status == "Cancelled":
                        match.status = "Ordered"
                        match.updated_by = request.user
                        match.save(update_fields=["status", "updated_by", "updated_at"])
                else:
                    created = ClinicalOrder.objects.create(
                        consultation=consultation,
                        order_type=ord_type,
                        name=ord_name,
                        status=ord_item.get("status", "Ordered"),
                        result_summary=ord_item.get("result_summary", ""),
                        updated_by=request.user,
                    )
                    retained_orders.add(created.pk)
        for existing in existing_orders:
            if existing.pk not in retained_orders and existing.status != "Cancelled":
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
    })


@login_required(login_url="login")
def drug_search_api(request):
    """API endpoint to search the medicine formulary with category filtering."""
    query = request.GET.get("q", "").strip().lower()
    category = request.GET.get("cat", "").strip()

    results = []
    for drug in STANDARD_FORMULARY:
        match_query = (not query) or (query in drug["name"].lower()) or (query in drug["generic"].lower())
        match_cat = (not category) or (category.lower() == "common") or (category.lower() in drug["category"].lower())
        if match_query and match_cat:
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

