from datetime import date

from django.shortcuts import render

from appointments.models import Appointment, Doctor
from patients.models import Patient


def dashboard(request):
    """Home page: today's schedule and key numbers."""
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
