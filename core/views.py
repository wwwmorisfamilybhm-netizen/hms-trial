from datetime import date, timedelta

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie


from .models import UserProfile


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
