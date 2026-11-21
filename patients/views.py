from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import PatientBillForm, PatientEditForm, PatientForm
from .models import Patient, PatientBill


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
            | Q(uhid__icontains=query)
            | Q(care_of__icontains=query)
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
    designation = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    can_push_bill = designation == "doctor"
    appointments = patient.appointments.select_related("doctor").order_by("-date", "-start_time")
    from patients.models import NurseTask
    open_tasks = list(
        NurseTask.objects.filter(patient=patient, status__in=["pending", "in_progress"])
        .select_related("assigned_to")
        .order_by("-priority", "due_at", "created_at")[:8]
    )
    context = {
        "patient": patient,
        "appointments": appointments,
        "can_view_appointments": True,
        "can_push_bill": can_push_bill,
        "bill_form": PatientBillForm(),
        "open_tasks": open_tasks,
    }
    return render(request, "patients/patient_detail.html", context)


@login_required(login_url="login")
def patient_push_bill(request, pk):
    """Allow doctor-designated users to add a pending patient charge."""
    designation = (
        getattr(getattr(request.user, "profile", None), "designation", None)
        or request.session.get("designation")
    )
    if designation != "doctor":
        messages.warning(request, "Only doctor-designated users can add a doctor bill.")
        return redirect("patient_detail", pk=pk)

    patient = get_object_or_404(Patient, pk=pk)
    form = PatientBillForm(request.POST)
    if form.is_valid():
        bill = form.save(commit=False)
        bill.patient = patient
        bill.source = PatientBill.Source.DOCTOR
        bill.created_by = request.user
        from .models import ConsultationSession
        bill.consultation = (
            patient.consultations.filter(
                status__in=[
                    ConsultationSession.Status.DRAFT,
                    ConsultationSession.Status.IN_PROGRESS,
                    ConsultationSession.Status.COMPLETED,
                ]
            )
            .order_by("-created_at")
            .first()
        )
        bill.save()
        messages.success(request, f"Pending bill {bill.bill_number} sent to Bill Command Center.")
    else:
        messages.error(request, "Could not add bill: correct the description, amount, or discount and try again.")
    return redirect("patient_detail", pk=patient.pk)


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
    saved_name = " ".join(filter(None, (patient.first_name, patient.second_name, patient.last_name)))
    saved_initials = patient.first_name[:1] + patient.last_name[:1]
    if request.method == "POST":
        form = PatientEditForm(request.POST, request.FILES, instance=patient)
        if form.is_valid():
            form.save()
            messages.success(request, f"Patient {patient.full_name} updated.")
            return redirect("patient_detail", pk=patient.pk)
    else:
        form = PatientEditForm(instance=patient)
    context = {
        "form": form,
        "patient": patient,
        "saved_name": saved_name,
        "saved_initials": saved_initials,
    }
    return render(request, "patients/patient_edit.html", context)


@login_required(login_url="login")
def patient_delete(request, pk):
    """Delete a patient record — the operator must re-enter their login password.

    A wrong password cancels the deletion and leaves the record untouched.
    """
    patient = get_object_or_404(
        Patient.objects.prefetch_related("appointments"), pk=pk
    )
    appointment_count = patient.appointments.count()

    if request.method == "POST":
        password = request.POST.get("password", "")
        if not request.user.check_password(password):
            messages.error(
                request,
                "Incorrect password. Deletion cancelled — the patient record is unchanged.",
            )
        else:
            label = f"{patient.full_name} (UHID #{patient.uhid or patient.id})"
            from django.db.models.deletion import ProtectedError
            try:
                patient.delete()
            except ProtectedError:
                messages.error(request, "This patient has pharmacy issues or service reports that must be retained. The record was not deleted.")
                return redirect("patient_detail", pk=patient.pk)
            messages.success(request, f"Patient record {label} was permanently deleted.")
            return redirect("history")

    return render(request, "patients/patient_delete.html", {
        "patient": patient,
        "appointment_count": appointment_count,
    })
