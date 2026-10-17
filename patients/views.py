from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import PatientForm
from .models import Patient


def patient_list(request):
    """List all patients, with a search box."""
    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all()
    if query:
        patients = patients.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone__icontains=query)
        )
    context = {
        "patients": patients,
        "query": query,
    }
    return render(request, "patients/patient_list.html", context)


def patient_detail(request, pk):
    """One patient's record and visit history."""
    patient = get_object_or_404(Patient, pk=pk)
    appointments = patient.appointments.select_related("doctor").order_by(
        "-date", "-start_time"
    )
    context = {
        "patient": patient,
        "appointments": appointments,
    }
    return render(request, "patients/patient_detail.html", context)


def patient_create(request):
    """Register a new patient."""
    if request.method == "POST":
        form = PatientForm(request.POST)
        if form.is_valid():
            patient = form.save()
            messages.success(request, f"Patient {patient.full_name} registered.")
            return redirect("patient_detail", pk=patient.pk)
    else:
        form = PatientForm()
    context = {
        "form": form,
        "title": "Register new patient",
    }
    return render(request, "patients/patient_form.html", context)
