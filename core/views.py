from datetime import date, timedelta

from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.http import JsonResponse
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
from patients.models import Patient, PatientBill


# ── Login / logout ────────────────────────────────────────────────────────────

DESIGNATION_CHOICES = [
    ("registration_desk", "Registration Desk"),
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
    if designation not in (UserProfile.Designation.DOCTOR, UserProfile.Designation.ADMIN) \
            and not request.user.is_superuser:
        messages.warning(request, "Access restricted: only doctors can assign a patient.")
        return redirect("dashboard")

    if request.method != "POST":
        return redirect("dashboard")

    doctor_id = request.POST.get("doctor_id", "").strip()
    if not doctor_id:
        patient.assigned_doctor = None
        patient.save(update_fields=["assigned_doctor"])
        messages.success(
            request, f"{patient.full_name} is now unassigned."
        )
        return redirect("dashboard")

    doctor = get_object_or_404(
        get_user_model(),
        pk=doctor_id,
        profile__designation=UserProfile.Designation.DOCTOR,
    )
    patient.assigned_doctor = doctor
    patient.save(update_fields=["assigned_doctor"])
    specialty = doctor.profile.get_specialty_display() if doctor.profile.specialty else "General Medicine"
    messages.success(
        request,
        f"{patient.full_name} assigned to {doctor.get_full_name() or doctor.username} — {specialty}.",
    )
    return redirect("dashboard")


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
    profile = getattr(request.user, "profile", None)
    user_desig = getattr(profile, "designation", None)
    if user_desig == UserProfile.Designation.ADMIN:
        return None
    messages.warning(request, "Access restricted: Hospital administrators only.")
    if user_desig == UserProfile.Designation.REGISTRATION_DESK:
        return redirect("reception_dashboard")
    return redirect("dashboard")


def _require_registration_desk(request):
    designation = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if designation == UserProfile.Designation.REGISTRATION_DESK:
        return None
    messages.warning(request, "This console is available to Registration Desk staff only.")
    if designation == UserProfile.Designation.ADMIN:
        return redirect("admin_dashboard")
    return redirect("dashboard")


@login_required(login_url="login")
def bill_command_center(request):
    """Find a patient and show the billing amounts currently stored on their record."""
    denied = _require_registration_desk(request)
    if denied:
        return denied

    from django.db.models import Q

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


# ── Registration Desk ─────────────────────────────────────────────────────────

@login_required(login_url="login")
def reception_dashboard(request):
    """Dedicated dashboard for registration desk."""
    # Block Doctor/Nurse users from accessing registration desk dashboard
    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
    if user_desig in ["doctor", "nurse"] and not request.user.is_superuser:
        from django.contrib import messages
        messages.warning(request, "Access restricted: Doctor/Nurse users should use the Doctor Dashboard.")
        return redirect("dashboard")

    today = date.today()
    todays_patients = Patient.objects.filter(created_at__date=today).order_by("-created_at")
    recent_patients = Patient.objects.order_by("-created_at")[:8]
    return render(request, "core/reception_dashboard.html", {
        "today": today,
        "total_patients": Patient.objects.count(),
        "today_count": todays_patients.count(),
        "todays_patients": todays_patients,
        "recent_patients": recent_patients,
        "user": request.user,
    })


@login_required(login_url="login")
def registration_desk(request):
    """Registration desk landing — new patient registration form."""
    user_desig = getattr(getattr(request.user, "profile", None), "designation", None) or request.session.get("designation")
    if user_desig in ["doctor", "nurse"] and not request.user.is_superuser:
        from django.contrib import messages
        messages.warning(request, "Access restricted: Patient Registration counter is for Registration Desk staff only.")
        return redirect("dashboard")
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

    return render(request, "core/registration_desk.html", {
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

    item_type = "patients" if is_registration_desk else request.GET.get("type", "all")
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
    include_appointments = not is_registration_desk and item_type in ("all", "appointments")

    rows = []

    if include_patients:
        patients = Patient.objects.all()
        if query:
            patients = patients.filter(
                Q(first_name__icontains=query)
                | Q(second_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(mr_number__icontains=query)
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
                | Q(doctor__first_name__icontains=query)
                | Q(doctor__last_name__icontains=query)
                | Q(reason__icontains=query)
            )
        if selected_date:
            appointments = appointments.filter(date=selected_date)
        if status:
            appointments = appointments.filter(status=status)
        for appointment in appointments:
            from django.utils import timezone
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
    rows = rows[:200]

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
    req = _urllib_request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    with _urllib_request.urlopen(req, timeout=30) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
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
    with _urllib_request.urlopen(req, timeout=30) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
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

    provider = os.environ.get("MEDGEMMA_PROVIDER", "mock").strip().lower()

    result = None
    fallback_reason = None
    if provider not in ("mock", "local", ""):
        try:
            if provider == "gemini":
                result = _gemini_draft(vitals, complaints, allergies, patient_name)
            else:
                result = _openai_compatible_draft(vitals, complaints, allergies, patient_name)
        except Exception as exc:  # noqa: BLE001 — any failure should fall back gracefully
            fallback_reason = str(exc)
            result = None

    if result is None:
        result = _medgemma_mock_draft(vitals, complaints, allergies)
        if fallback_reason:
            result["_fallback"] = fallback_reason

    if not (result.get("summary") and result.get("differential") and result.get("tests")):
        result = _medgemma_mock_draft(vitals, complaints, allergies)

    return JsonResponse(result)


@login_required(login_url="login")
@require_POST
def save_initial_assessment(request):
    """Persist an OPD initial assessment for a patient (create or update)."""
    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    patient_id = payload.get("patient_id")
    if not patient_id:
        return JsonResponse({"ok": False, "error": "patient_id required"}, status=400)

    from patients.models import InitialAssessment, Patient
    patient = Patient.objects.filter(pk=patient_id).first()
    if not patient:
        return JsonResponse({"ok": False, "error": "Patient not found"}, status=404)

    def _num(v, cast=int):
        if v is None or v == "":
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    assessment, _ = InitialAssessment.objects.get_or_create(patient=patient)

    assessment.temperature = _num(payload.get("temperature"), float)
    assessment.pulse = _num(payload.get("pulse"))
    assessment.respiratory_rate = _num(payload.get("respiratory_rate"))
    assessment.bp_systolic = _num(payload.get("bp_systolic"))
    assessment.bp_diastolic = _num(payload.get("bp_diastolic"))
    assessment.spo2 = _num(payload.get("spo2"))
    assessment.height = _num(payload.get("height"), float)
    assessment.weight = _num(payload.get("weight"), float)
    assessment.bmi = _num(payload.get("bmi"), float)
    assessment.rbs = _num(payload.get("rbs"))
    assessment.pain_score = _num(payload.get("pain_score"))

    assessment.complaints = payload.get("complaints", "") or ""
    assessment.hpi = payload.get("hpi", "") or ""
    assessment.summary_note = payload.get("summary_note", "") or ""

    assessment.chronic_conditions = payload.get("chronic_conditions", []) or []
    assessment.allergies = payload.get("allergies", []) or []
    assessment.medications = payload.get("medications", []) or []
    assessment.social_history = payload.get("social_history", []) or []
    assessment.examination = payload.get("examination", {}) or {}

    assessment.save()
    return JsonResponse({"ok": True, "id": assessment.pk})


@login_required(login_url="login")
def initial_assessment(request):
    """OPD triage workstation — select a patient, then record vitals, complaints
    and history before consultation.

    Renders inside the standard app shell (sidebar on the left). When no
    ``patient`` id is supplied the page shows a patient-picker so the nurse can
    choose whom to assess; once chosen the assessment form is shown.
    """
    import json

    from django.db.models import Q
    from django.utils import timezone

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

    # Patient search / picker list
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
    patients = patients.order_by("-created_at")[:30]

    # Selected patient (if any)
    patient = None
    patient_id = request.GET.get("patient", "").strip()
    if patient_id.isdigit():
        patient = Patient.objects.filter(pk=int(patient_id)).first()

    patient_ctx = None
    allergies_json = "[]"
    assessment_json = "null"
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
            "name": patient.full_name or "—",
            "initials": initials,
            "uhid": patient.uhid,
            "visit_token": f"VST-{now:%y%m%d}-{patient.pk:04d}",
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
        allergies_json = json.dumps(patient_ctx["allergies"])

        # Load any previously saved assessment for pre-filling the form
        from patients.models import InitialAssessment
        try:
            a = patient.initial_assessment
            assessment_json = _json.dumps({
                "temperature": (str(a.temperature) if a.temperature is not None else ""),
                "pulse": (a.pulse if a.pulse is not None else ""),
                "respiratory_rate": (a.respiratory_rate if a.respiratory_rate is not None else ""),
                "bp_systolic": (a.bp_systolic if a.bp_systolic is not None else ""),
                "bp_diastolic": (a.bp_diastolic if a.bp_diastolic is not None else ""),
                "spo2": (a.spo2 if a.spo2 is not None else ""),
                "height": (str(a.height) if a.height is not None else ""),
                "weight": (str(a.weight) if a.weight is not None else ""),
                "bmi": (str(a.bmi) if a.bmi is not None else ""),
                "rbs": (a.rbs if a.rbs is not None else ""),
                "pain_score": (a.pain_score if a.pain_score is not None else ""),
                "complaints": a.complaints or "",
                "hpi": a.hpi or "",
                "summary_note": a.summary_note or "",
                "chronic_conditions": a.chronic_conditions or [],
                "allergies": a.allergies or [],
                "medications": a.medications or [],
                "social_history": a.social_history or [],
                "examination": a.examination or {},
            })
        except InitialAssessment.DoesNotExist:
            assessment_json = "null"

    return render(request, "core/initial_assessment.html", {
        "patient_ctx": patient_ctx,
        "allergies_json": allergies_json,
        "assessment_json": assessment_json,
        "has_patient": patient is not None,
        "selected_patient": patient,
        "patients": patients,
        "query": query,
        "user": request.user,
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
