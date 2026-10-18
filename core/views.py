from datetime import date, timedelta

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
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
from patients.models import Patient


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
    }
    return render(request, "core/dashboard.html", context)


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


@login_required(login_url="login")
def admin_settings(request):
    """Admin-only settings landing page."""
    denied = _require_admin(request)
    if denied:
        return denied

    return render(request, "core/admin_settings.html")


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
    todays_appointments = (
        Appointment.objects.filter(date=today)
        .select_related("patient", "doctor")
        .order_by("start_time")
    )

    return render(request, "core/reception_dashboard.html", {
        "today": today,
        "total_patients": Patient.objects.count(),
        "today_count": todays_patients.count(),
        "todays_patients": todays_patients,
        "todays_appointments": todays_appointments,
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
            patient = form.save(commit=False)
            patient.mr_number = str(next_mr)

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
                    filename = f"patient_{next_mr}_{clean_name}.{ext}"
                    patient.photo.save(filename, ContentFile(base64.b64decode(imgstr)), save=False)
                except Exception:
                    pass

            patient.save()
            return redirect("patient_print_card", patient_id=patient.pk)
    else:
        from patients.forms import PatientRegistrationForm
        form = PatientRegistrationForm(initial={
            "nationality": "India",
            "reg_fee": 50,
            "con_fee": 0,
            "discount": 0,
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
                "view_url": reverse("patient_detail", args=[patient.pk]),
                "edit_url": reverse("patient_edit", args=[patient.pk]),
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
        "user": request.user,
    })
