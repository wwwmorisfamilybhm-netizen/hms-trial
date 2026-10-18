from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import PatientForm, PatientRegistrationForm
from .models import Patient


@login_required(login_url="login")
def patient_list(request):
    """List all patients with registration details and search."""
    query = request.GET.get("q", "").strip()
    patients = Patient.objects.all().order_by("-created_at")
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
    context = {
        "patients": patients,
        "query": query,
    }
    return render(request, "patients/patient_list.html", context)


@login_required(login_url="login")
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


@login_required(login_url="login")
def patient_create(request):
    """Register a new patient."""
    user_desig = getattr(getattr(request.user, "profile", None), "designation", None)
    if user_desig == "registration_desk":
        return redirect("registration_desk")

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


@login_required(login_url="login")
def patient_edit(request, pk):
    """Edit a registered patient's full record."""
    patient = get_object_or_404(Patient, pk=pk)
    if request.method == "POST":
        form = PatientRegistrationForm(request.POST, request.FILES, instance=patient)
        if form.is_valid():
            form.save()
            messages.success(request, f"Patient {patient.full_name} updated.")
            return redirect("patient_detail", pk=patient.pk)
    else:
        form = PatientRegistrationForm(instance=patient)
    context = {
        "form": form,
        "title": f"Edit patient — {patient.full_name}",
    }
    return render(request, "patients/patient_form.html", context)
