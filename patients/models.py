from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models


class Patient(models.Model):
    """A registered hospital patient."""

    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"
        OTHER = "O", "Other"

    BLOOD_GROUPS = [
        ("A+", "A+"), ("A-", "A-"),
        ("B+", "B+"), ("B-", "B-"),
        ("AB+", "AB+"), ("AB-", "AB-"),
        ("O+", "O+"), ("O-", "O-"),
    ]

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUPS, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    allergies = models.TextField(
        blank=True,
        help_text="Known allergies (medications, food, etc.)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ── Registration-desk extended fields ──────────────────────────────────
    mr_number = models.CharField(max_length=20, blank=True, verbose_name="MR Number")
    second_name = models.CharField(max_length=100, blank=True, verbose_name="Second Name")
    centre = models.CharField(
        max_length=80, blank=True, verbose_name="Centre",
        help_text="Registration centre (CHS, Camp, …) — managed via registration options",
    )

    class MaritalStatus(models.TextChoices):
        SINGLE = "single", "Single"
        MARRIED = "married", "Married"
        WIDOWED = "widowed", "Widowed"
        DIVORCED = "divorced", "Divorced"

    marital_status = models.CharField(
        max_length=10, blank=True, choices=MaritalStatus.choices, verbose_name="Marital Status"
    )
    aadhar_no = models.CharField(max_length=20, blank=True, verbose_name="Aadhar No.")
    abha_no = models.CharField(max_length=30, blank=True, verbose_name="ABHA No")
    care_of = models.CharField(max_length=150, blank=True, verbose_name="C/O")
    place_panch = models.CharField(max_length=150, blank=True, verbose_name="Place/Panch.")

    # Address details
    nationality = models.CharField(max_length=60, blank=True, default="India", verbose_name="Nationality")
    state = models.CharField(max_length=80, blank=True, verbose_name="State")
    district = models.CharField(max_length=80, blank=True, verbose_name="District")
    pin_code = models.CharField(max_length=10, blank=True, verbose_name="Pin Code")
    zone = models.CharField(max_length=60, blank=True, verbose_name="Zone")
    block = models.CharField(max_length=60, blank=True, verbose_name="Block")
    village = models.CharField(max_length=100, blank=True)
    religion = models.CharField(max_length=60, blank=True, verbose_name="Religion")

    # Billing & fees
    reg_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name="Reg Fee")
    con_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name="Con Fee")
    discount = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name="Discount")

    class PaymentType(models.TextChoices):
        CASH = "cash", "Cash"
        CREDIT = "credit", "Credit"
        FREE = "free", "Free"
        CARD = "card", "Card"
        PART = "part", "Part"
        PENDING = "pending", "Pending (Payment Deferred)"

    payment_type = models.CharField(
        max_length=10, blank=True, default="cash", choices=PaymentType.choices, verbose_name="Payment Type"
    )

    class PatientCategory(models.TextChoices):
        GENERAL = "GENERAL", "General"
        BPL = "BPL", "BPL"
        ESI = "ESI", "ESI"
        GOVT = "GOVT", "Govt"
        VIP = "VIP", "VIP"

    patient_category = models.CharField(max_length=20, blank=True, default="GENERAL", verbose_name="Patient Category")
    reference_no = models.CharField(max_length=40, blank=True, verbose_name="Reference No.")
    valid_upto = models.DateField(null=True, blank=True, verbose_name="Valid Upto")

    class BillingType(models.TextChoices):
        INDIVIDUAL = "Individual", "Individual"
        CORPORATE = "Corporate", "Corporate"
        INSURANCE = "Insurance", "Insurance"

    billing_type = models.CharField(
        max_length=20, blank=True, default="Individual", choices=BillingType.choices, verbose_name="Billing Type"
    )
    mlc = models.BooleanField(default=False, verbose_name="MLC Regn.")
    is_emergency = models.BooleanField(
        default=False,
        verbose_name="Emergency Registration",
        help_text="Registered through the fast emergency intake counter; remaining details to be completed later.",
    )
    photo = models.FileField(upload_to="patient_photos/", blank=True, null=True, verbose_name="Patient Photo")
    assigned_doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_patients",
        verbose_name="Assigned Doctor",
        help_text="Doctor account (created in Admin Console → Doctor Management) looking after this patient.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def uhid(self):
        """Unique hospital patient ID — prefers the registered MR number."""
        if self.mr_number:
            return self.mr_number
        return f"UHID{self.pk:06d}" if self.pk else "UHID—"

    @property
    def age(self):
        """Age in years, or None if date of birth is missing."""
        if not self.date_of_birth:
            return None
        today = date.today()
        return (
            today.year
            - self.date_of_birth.year
            - ((today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day))
        )


class PatientCategoryConfig(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=80)
    registration_fee = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name = "Patient category"
        verbose_name_plural = "Patient categories"

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.registration_fee is not None and self.registration_fee < Decimal("0"):
            raise ValidationError({"registration_fee": "Fee cannot be negative."})


class RegistrationFeeSettings(models.Model):
    default_registration_fee = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    default_consultation_fee = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    default_discount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        from django.core.exceptions import ValidationError
        for field_name in ("default_registration_fee", "default_consultation_fee", "default_discount"):
            value = getattr(self, field_name)
            if value is not None and value < Decimal("0"):
                raise ValidationError({field_name: "Amount cannot be negative."})

    def __str__(self):
        return "Default registration fees"


class RegistrationOption(models.Model):
    class OptionType(models.TextChoices):
        STATE = "state", "State"
        RELIGION = "religion", "Religion"
        CENTRE = "centre", "Centre"

    option_type = models.CharField(max_length=10, choices=OptionType.choices)
    name = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["display_order", "name"]
        constraints = [models.UniqueConstraint(fields=["option_type", "name"], name="unique_registration_option")]

    def __str__(self):
        return f"{self.get_option_type_display()}: {self.name}"


class PatientBill(models.Model):
    class Source(models.TextChoices):
        REGISTRATION = "registration", "Registration"
        DOCTOR = "doctor", "Doctor"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="bills")
    source = models.CharField(max_length=20, choices=Source.choices)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="patient_bills_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "created_at"]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.amount < Decimal("0"):
            raise ValidationError({"amount": "Amount cannot be negative."})
        if self.discount < Decimal("0"):
            raise ValidationError({"discount": "Discount cannot be negative."})
        if self.discount > self.amount:
            raise ValidationError({"discount": "Discount cannot exceed the bill amount."})

    @property
    def amount_due(self):
        return max(self.amount - self.discount, Decimal("0"))

    @property
    def bill_number(self):
        return f"BILL-{self.pk:06d}" if self.pk else "New bill"

    def __str__(self):
        return f"{self.bill_number} — {self.patient.full_name}"


class InitialAssessment(models.Model):
    """OPD triage assessment recorded before doctor consultation."""

    patient = models.OneToOneField(
        Patient, on_delete=models.CASCADE, related_name="initial_assessment",
        verbose_name="Patient",
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Vitals
    temperature = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    pulse = models.PositiveIntegerField(null=True, blank=True)
    respiratory_rate = models.PositiveIntegerField(null=True, blank=True)
    bp_systolic = models.PositiveIntegerField(null=True, blank=True)
    bp_diastolic = models.PositiveIntegerField(null=True, blank=True)
    spo2 = models.PositiveIntegerField(null=True, blank=True)
    height = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    weight = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    bmi = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    rbs = models.PositiveIntegerField(null=True, blank=True)
    pain_score = models.PositiveSmallIntegerField(null=True, blank=True)

    # Narrative
    complaints = models.TextField(blank=True, default="")
    hpi = models.TextField(blank=True, default="")
    summary_note = models.TextField(blank=True, default="")

    # Structured (JSON)
    chronic_conditions = models.JSONField(default=list, blank=True)
    allergies = models.JSONField(default=list, blank=True)
    medications = models.JSONField(default=list, blank=True)
    social_history = models.JSONField(default=list, blank=True)
    examination = models.JSONField(default=dict, blank=True)

    # Orders requested during triage: {"laboratory": [...], "radiology": [...], "other": [...]}
    orders = models.JSONField(default=dict, blank=True)

    # Previous surgeries / hospitalisations
    surgeries = models.TextField(blank=True, default="")

    class Meta:
        verbose_name = "Initial Assessment"
        verbose_name_plural = "Initial Assessments"

    def __str__(self):
        return f"Initial Assessment — {self.patient.full_name}"


class ConsultationSession(models.Model):
    """A doctor consultation encounter in Doctor's Desk."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class VisitType(models.TextChoices):
        NEW_PROBLEM = "new_problem", "New Problem"
        FOLLOW_UP_SAME = "follow_up_same", "Follow-up (Same Problem)"
        FOLLOW_UP_DIFF = "follow_up_diff", "Follow-up (Different Problem)"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="consultations", verbose_name="Patient"
    )
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="doctor_consultations",
        verbose_name="Doctor",
    )
    visit_token = models.CharField(max_length=50, blank=True, verbose_name="Visit Token")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, verbose_name="Status"
    )
    visit_type = models.CharField(
        max_length=20, choices=VisitType.choices, default=VisitType.NEW_PROBLEM, verbose_name="Visit Type"
    )

    # Clinical Notes & Objective findings
    clinical_note = models.TextField(blank=True, default="", verbose_name="Doctor's Clinical Note")
    examination = models.JSONField(default=dict, blank=True, verbose_name="Examination Findings")

    # Diagnosis & Plan
    diagnosis_primary = models.CharField(max_length=255, blank=True, default="", verbose_name="Primary Diagnosis")
    diagnosis_primary_icd = models.CharField(max_length=30, blank=True, default="", verbose_name="ICD Code")
    diagnosis_secondary = models.JSONField(default=list, blank=True, verbose_name="Secondary Diagnoses")
    treatment_plan = models.TextField(blank=True, default="", verbose_name="Treatment Plan")

    # Advice & Follow-up
    special_advice = models.TextField(blank=True, default="", verbose_name="Special Advice")
    patient_education = models.JSONField(default=list, blank=True, verbose_name="Patient Education")
    mark_follow_up = models.BooleanField(default=False, verbose_name="Mark for Follow-up")
    follow_up_after = models.CharField(max_length=50, blank=True, default="", verbose_name="Follow-up After")
    follow_up_date = models.DateField(null=True, blank=True, verbose_name="Follow-up Date")
    additional_orders = models.JSONField(default=list, blank=True, verbose_name="Additional Orders")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Consultation Session"
        verbose_name_plural = "Consultation Sessions"

    def __str__(self):
        return f"Consultation #{self.pk} — {self.patient.full_name} ({self.get_status_display()})"


class PrescriptionItem(models.Model):
    """An individual prescribed medication in a consultation."""

    consultation = models.ForeignKey(
        ConsultationSession, on_delete=models.CASCADE, related_name="prescriptions"
    )
    medicine_name = models.CharField(max_length=255, verbose_name="Medicine")
    dose = models.CharField(max_length=100, blank=True, default="", verbose_name="Dose")
    route = models.CharField(max_length=50, default="Oral", verbose_name="Route")
    frequency = models.CharField(max_length=50, default="OD", verbose_name="Frequency")
    duration = models.CharField(max_length=50, default="5 days", verbose_name="Duration")
    instructions = models.CharField(max_length=255, blank=True, default="", verbose_name="Instructions")
    order_index = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order_index", "id"]
        verbose_name = "Prescription Item"
        verbose_name_plural = "Prescription Items"

    def __str__(self):
        return f"{self.medicine_name} {self.dose} ({self.frequency})"


class ClinicalOrder(models.Model):
    """Laboratory, Radiology, or Procedure order placed during consultation."""

    class OrderType(models.TextChoices):
        LABORATORY = "laboratory", "Laboratory"
        RADIOLOGY = "radiology", "Radiology / Imaging"
        OTHER = "other", "Other Services"

    consultation = models.ForeignKey(
        ConsultationSession, on_delete=models.CASCADE, related_name="orders"
    )
    order_type = models.CharField(max_length=30, choices=OrderType.choices, default=OrderType.LABORATORY)
    name = models.CharField(max_length=255, verbose_name="Service / Test")
    status = models.CharField(max_length=30, default="Ordered", verbose_name="Status")
    result_summary = models.CharField(max_length=255, blank=True, default="", verbose_name="Result Summary")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Clinical Order"
        verbose_name_plural = "Clinical Orders"

    def __str__(self):
        return f"{self.name} ({self.status})"


class DiagnosticReport(models.Model):
    """Diagnostic investigation report (Lab, X-Ray, ECG, USG, Documents) for a patient."""

    class Category(models.TextChoices):
        LABORATORY = "laboratory", "Laboratory"
        RADIOLOGY = "radiology", "Radiology"
        CARDIOLOGY = "cardiology", "Cardiology"
        DOCUMENTS = "documents", "Documents"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="diagnostic_reports", verbose_name="Patient"
    )
    category = models.CharField(max_length=30, choices=Category.choices, default=Category.LABORATORY)
    title = models.CharField(max_length=255, verbose_name="Report Title")
    status = models.CharField(max_length=30, default="Completed", verbose_name="Status")
    details = models.JSONField(default=dict, blank=True, verbose_name="Report Details")
    report_date = models.DateTimeField(auto_now_add=True, verbose_name="Report Date")

    class Meta:
        ordering = ["-report_date"]
        verbose_name = "Diagnostic Report"
        verbose_name_plural = "Diagnostic Reports"

    def __str__(self):
        return f"{self.title} — {self.patient.full_name} ({self.category})"

