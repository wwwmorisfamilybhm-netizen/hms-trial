from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import AppointmentForm
from .models import Appointment


@login_required(login_url="login")
def appointment_list(request):
    """Appointments for a chosen date (defaults to today)."""
    date_str = request.GET.get("date", "")
    try:
        selected = (
            datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
        )
    except ValueError:
        selected = date.today()

    appointments = (
        Appointment.objects.filter(date=selected)
        .select_related("patient", "doctor")
        .order_by("start_time")
    )
    context = {
        "appointments": appointments,
        "selected_date": selected,
        "prev_date": selected - timedelta(days=1),
        "next_date": selected + timedelta(days=1),
        "is_today": selected == date.today(),
    }
    return render(request, "appointments/appointment_list.html", context)


@login_required(login_url="login")
def appointment_create(request):
    """Book a new appointment."""
    initial = {}
    if request.GET.get("patient"):
        initial["patient"] = request.GET["patient"]

    if request.method == "POST":
        form = AppointmentForm(request.POST)
        if form.is_valid():
            appointment = form.save()
            messages.success(
                request,
                f"Appointment booked: {appointment.patient} with "
                f"{appointment.doctor} on {appointment.date} at "
                f"{appointment.start_time:%H:%M}.",
            )
            return redirect("appointment_list")
    else:
        form = AppointmentForm(initial=initial)
    context = {
        "form": form,
        "title": "Book appointment",
    }
    return render(request, "appointments/appointment_form.html", context)


@login_required(login_url="login")
def appointment_edit(request, pk):
    """Edit an existing appointment (re-uses the booking form)."""
    appointment = get_object_or_404(
        Appointment.objects.select_related("patient", "doctor"), pk=pk
    )
    if request.method == "POST":
        form = AppointmentForm(request.POST, instance=appointment)
        if form.is_valid():
            appointment = form.save()
            messages.success(
                request,
                f"Appointment updated: {appointment.patient} with "
                f"{appointment.doctor} on {appointment.date} at "
                f"{appointment.start_time:%H:%M}.",
            )
            return redirect("appointment_list")
    else:
        form = AppointmentForm(instance=appointment)
    context = {
        "form": form,
        "title": "Edit appointment",
        "submit_label": "Save changes",
    }
    return render(request, "appointments/appointment_form.html", context)
