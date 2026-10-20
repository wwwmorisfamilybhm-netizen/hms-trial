from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from patients.models import Patient

from .forms import AppointmentForm
from .models import Appointment, Doctor


@login_required(login_url="login")
def appointment_list(request):
    """Appointment desk: patient search for instant booking + appointment history."""
    date_str = request.GET.get("date", "")
    query = (request.GET.get("q") or "").strip()
    status_filter = request.GET.get("status", "")
    doctor_filter = request.GET.get("doctor", "")

    # With a search term the history spans every date; without one it shows
    # the day schedule (defaults to today).
    if query:
        selected = None
        base = (
            Appointment.objects.filter(
                Q(patient__first_name__icontains=query)
                | Q(patient__last_name__icontains=query)
                | Q(patient__mr_number__icontains=query)
                | Q(patient__phone__icontains=query)
                | Q(doctor__first_name__icontains=query)
                | Q(doctor__last_name__icontains=query)
            )
            .select_related("patient", "doctor")
            .order_by("-date", "start_time")[:100]
        )
    else:
        try:
            selected = (
                datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
            )
        except ValueError:
            selected = date.today()
        base = Appointment.objects.filter(date=selected).select_related("patient", "doctor")

    appointments = list(base)
    stats = {
        "total": len(appointments),
        "scheduled": sum(a.status == Appointment.Status.SCHEDULED for a in appointments),
        "completed": sum(a.status == Appointment.Status.COMPLETED for a in appointments),
        "cancelled": sum(
            a.status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW)
            for a in appointments
        ),
    }
    if status_filter:
        appointments = [a for a in appointments if a.status == status_filter]
    if doctor_filter:
        appointments = [a for a in appointments if str(a.doctor_id) == doctor_filter]

    nav = {"q": query, "status": status_filter, "doctor": doctor_filter}
    context = {
        "appointments": appointments,
        "search_mode": bool(query),
        "query": query,
        "status_filter": status_filter,
        "doctor_filter": doctor_filter,
        "selected_date": selected,
        "prev_date": (selected - timedelta(days=1)) if selected else None,
        "next_date": (selected + timedelta(days=1)) if selected else None,
        "is_today": selected == date.today(),
        "nav": nav,
        "stats": stats,
        "status_choices": Appointment.Status.choices,
        "doctors": Doctor.objects.order_by("last_name", "first_name"),
        "can_manage_appointments": True,
    }
    return render(request, "appointments/appointment_list.html", context)


@login_required(login_url="login")
def appointment_patient_search(request):
    """JSON patient search used by the appointment desk booking panel."""
    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})

    matches = (
        Patient.objects.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(second_name__icontains=query)
            | Q(mr_number__icontains=query)
            | Q(phone__icontains=query)
        )
        .order_by("last_name", "first_name")[:8]
    )
    results = [
        {
            "pk": patient.pk,
            "name": patient.full_name,
            "uhid": patient.uhid,
            "age": patient.age or "",
            "gender": patient.get_gender_display(),
            "phone": patient.phone or "",
            "booking_url": f"{reverse('appointment_create')}?patient={patient.pk}",
        }
        for patient in matches
    ]
    return JsonResponse({"results": results})


@login_required(login_url="login")
def appointment_create(request):
    """Book a new appointment for a searched or selected patient."""
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
