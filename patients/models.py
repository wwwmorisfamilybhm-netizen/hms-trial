from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models
from .storage import private_report_storage, report_upload_path, pharmacy_import_path


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
    is_demo = models.BooleanField(default=False, help_text="Synthetic demonstration record; never use as real patient data.")

    # ── Registration-desk extended fields ──────────────────────────────────
    uhid = models.CharField(max_length=20, blank=True, verbose_name="UHID")
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


class AppointmentFeeSettings(models.Model):
    """Default pricing applied to every appointment booking.

    Kept separate from the registration fees so appointment pricing can be
    tuned independently — e.g. Appointment ₹20 + Consultation ₹100 −
    Discount ₹100 = ₹20 due per appointment.
    """

    appointment_fee = models.DecimalField(max_digits=8, decimal_places=2, default=20, verbose_name="Appointment Fee (₹)")
    consultation_fee = models.DecimalField(max_digits=8, decimal_places=2, default=100, verbose_name="Consultation Fee (₹)")
    discount = models.DecimalField(max_digits=8, decimal_places=2, default=100, verbose_name="Discount (₹)")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Appointment Fee Settings"
        verbose_name_plural = "Appointment Fee Settings"

    def clean(self):
        from django.core.exceptions import ValidationError
        for field_name in ("appointment_fee", "consultation_fee", "discount"):
            value = getattr(self, field_name)
            if value is not None and value < Decimal("0"):
                raise ValidationError({field_name: "Amount cannot be negative."})

    @property
    def gross(self):
        return (self.appointment_fee or Decimal("0")) + (self.consultation_fee or Decimal("0"))

    @property
    def net_due(self):
        """Amount actually charged per appointment, never below zero."""
        return max(self.gross - (self.discount or Decimal("0")), Decimal("0"))

    @classmethod
    def current(cls):
        """Return the singleton row, creating it with defaults on first use."""
        settings = cls.objects.order_by("pk").first()
        if settings is None:
            settings = cls.objects.create()
        return settings

    def __str__(self):
        return "Appointment fee settings"


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
        APPOINTMENT = "appointment", "Appointment"
        SERVICE = "service", "Service"
        PHARMACY = "pharmacy", "Pharmacy"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        UPI = "upi", "UPI"
        CREDIT = "credit", "Credit"
        OTHER = "other", "Other"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="bills")
    source = models.CharField(max_length=20, choices=Source.choices)
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    appointment = models.ForeignKey(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bills",
        verbose_name="Appointment / visit",
    )
    consultation = models.ForeignKey(
        "ConsultationSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bills",
        verbose_name="Consultation",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="patient_bills_created",
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="patient_bills_collected",
        verbose_name="Payment collected by",
    )
    payment_method = models.CharField(
        max_length=10,
        choices=PaymentMethod.choices,
        blank=True,
        default="",
        verbose_name="Payment method",
    )
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="Payment reference",
    )
    payment_group = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Payment group",
        help_text="Bills paid together in one cashier transaction share this reference.",
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
        self._validate_pharmacy_snapshot()

    def _validate_pharmacy_snapshot(self):
        from django.core.exceptions import ValidationError
        if self.pk:
            previous = PatientBill.objects.filter(pk=self.pk).first()
            issue = PharmacyIssue.objects.filter(bill_id=self.pk).first()
            if issue and previous:
                if self.status != previous.status:
                    raise ValidationError("Use Pharmacy Billing to record payment; cancel unpaid issues in Pharmacy.")
                if any(getattr(previous, field) != getattr(self, field) for field in ("amount", "discount", "patient_id", "source")):
                    raise ValidationError("Prepared pharmacy bills have fixed prices and patient details.")
                if issue.status == "dispensed" and self.status != "paid":
                    raise ValidationError("Payment cannot be reset after medicine handover.")
                if previous.status == "cancelled" and self.status != "cancelled":
                    raise ValidationError("A cancelled bill cannot be reopened.")

    def save(self, *args, **kwargs):
        self._validate_pharmacy_snapshot()
        super().save(*args, **kwargs)

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
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        COMPLETED = "completed", "Completed"

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.COMPLETED)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="completed_initial_assessments",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Vitals
    # Stored in Celsius; two decimals preserve a Fahrenheit reading to 0.1°F.
    temperature = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
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
    appointment = models.ForeignKey(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consultations",
        verbose_name="Appointment / visit",
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
    medicine = models.ForeignKey("Medicine", on_delete=models.PROTECT, null=True, blank=True)
    prescribed_quantity = models.PositiveIntegerField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    legacy_dispensed_quantity = models.PositiveIntegerField(default=0)
    reserved_quantity = models.PositiveIntegerField(default=0)
    dose = models.CharField(max_length=100, blank=True, default="", verbose_name="Dose")
    route = models.CharField(max_length=50, default="Oral", verbose_name="Route")
    frequency = models.CharField(max_length=50, default="OD", verbose_name="Frequency")
    duration = models.CharField(max_length=50, default="5 days", verbose_name="Duration")
    instructions = models.CharField(max_length=255, blank=True, default="", verbose_name="Instructions")
    class DispenseStatus(models.TextChoices):
        ORDERED = "ordered", "Ordered"
        PARTIAL = "partial", "Partially dispensed"
        DISPENSED = "dispensed", "Dispensed"
        UNAVAILABLE = "unavailable", "Unavailable"
        CANCELLED = "cancelled", "Cancelled"

    class StockStatus(models.TextChoices):
        AVAILABLE = "available", "Available"
        LOW = "low", "Low stock"
        UNAVAILABLE = "unavailable", "Unavailable"

    dispense_status = models.CharField(max_length=12, choices=DispenseStatus.choices, default=DispenseStatus.ORDERED)
    stock_status = models.CharField(max_length=12, choices=StockStatus.choices, default=StockStatus.AVAILABLE)
    dispensed_quantity = models.PositiveSmallIntegerField(default=0)
    dispensed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="opd_prescriptions_dispensed",
    )
    updated_at = models.DateTimeField(auto_now=True)
    order_index = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order_index", "id"]
        verbose_name = "Prescription Item"
        verbose_name_plural = "Prescription Items"

    def __str__(self):
        return f"{self.medicine_name} {self.dose} ({self.frequency})"

    def _validate_snapshot(self):
        from django.core.exceptions import ValidationError
        previous = PrescriptionItem.objects.filter(pk=self.pk).first() if self.pk else None
        if previous and (previous.released_at or previous.legacy_dispensed_quantity):
            fields = ("consultation_id", "medicine_id", "medicine_name", "prescribed_quantity", "dose", "route", "frequency", "duration", "instructions", "released_at", "dispensed_quantity", "reserved_quantity", "dispense_status", "legacy_dispensed_quantity", "dispensed_by_id")
            if any(getattr(previous, field) != getattr(self, field) for field in fields):
                raise ValidationError("Released prescriptions are read-only; use pharmacy operations or create an amendment.")

    def clean(self):
        self._validate_snapshot()

    def save(self, *args, **kwargs):
        self._validate_snapshot()
        super().save(*args, **kwargs)

    @property
    def remaining_quantity(self):
        return max((self.prescribed_quantity or 0) - self.dispensed_quantity - self.reserved_quantity, 0)


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
    department = models.ForeignKey("ServiceDepartment", on_delete=models.PROTECT, null=True, blank=True)
    service_definition = models.ForeignKey("ServiceDefinition", on_delete=models.PROTECT, null=True, blank=True)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="Price snapshot",
        help_text="Price charged for this order; copied from the service catalogue when ordered.",
    )
    bill = models.ForeignKey(
        "PatientBill",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_orders",
        verbose_name="Service bill",
    )
    assigned_staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_service_orders")
    priority = models.CharField(max_length=10, choices=[("routine", "Routine"), ("urgent", "Urgent")], default="routine")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="completed_service_orders")
    status = models.CharField(max_length=30, default="Ordered", verbose_name="Status")
    result_summary = models.CharField(max_length=255, blank=True, default="", verbose_name="Result Summary")
    sessions_planned = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Planned sessions",
        help_text="Physiotherapy and counselling: number of sessions expected in this plan.",
    )
    operational_details = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="opd_orders_updated",
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Clinical Order"
        verbose_name_plural = "Clinical Orders"

    def __str__(self):
        return f"{self.name} ({self.status})"

    def save(self, *args, **kwargs):
        if self.service_definition_id:
            self.department_id = self.service_definition.department_id
        elif not self.department_id:
            definition = ServiceDefinition.objects.filter(name__iexact=self.name, is_active=True).first()
            if definition:
                self.service_definition = definition
                self.department_id = definition.department_id
            elif self.order_type in ("laboratory", "radiology"):
                self.department = ServiceDepartment.objects.filter(slug=self.order_type).first()
        if self.service_definition_id and not self.price:
            self.price = self.service_definition.price
        super().save(*args, **kwargs)

    @property
    def payment_pending(self):
        return bool(self.bill_id and self.bill.status == PatientBill.Status.PENDING)

    @property
    def is_paid(self):
        return not self.bill_id or self.bill.status == PatientBill.Status.PAID

    @property
    def attended_session_count(self):
        if not self.pk:
            return 0
        return self.sessions.filter(status=ServiceSession.Status.ATTENDED).count()

    @property
    def session_progress_percent(self):
        if not self.sessions_planned:
            return None
        return min(round(self.attended_session_count * 100 / self.sessions_planned), 100)

    @property
    def is_session_service(self):
        return bool(self.department_id and self.department.slug in ("physiotherapy", "counselling"))


class ServiceSession(models.Model):
    """A scheduled appointment in a physiotherapy or counselling plan.

    The order keeps the clinical request and billing link; the sessions record
    attendance and progress over time. Notes on counselling sessions are
    restricted to the clinical team by the operations views.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        ATTENDED = "attended", "Attended"
        NO_SHOW = "no_show", "No-show"
        CANCELLED = "cancelled", "Cancelled"

    order = models.ForeignKey(
        ClinicalOrder, on_delete=models.CASCADE, related_name="sessions"
    )
    scheduled_at = models.DateTimeField(null=True, blank=True, verbose_name="Scheduled session")
    room = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.SCHEDULED, db_index=True
    )
    progress_note = models.TextField(
        blank=True,
        default="",
        help_text="Session progress. Counselling notes are restricted to the clinical team.",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_sessions_recorded",
    )
    recorded_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_sessions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scheduled_at", "id"]
        verbose_name = "Service Session"
        verbose_name_plural = "Service Sessions"

    def __str__(self):
        when = self.scheduled_at.strftime("%d %b %Y %H:%M") if self.scheduled_at else "unscheduled"
        return f"{self.order.name} · session #{self.pk or 'new'} · {when}"


class OPDAuditEvent(models.Model):
    """Append-only event record for changes made during an OPD visit."""

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="opd_audit_events")
    order = models.ForeignKey(
        ClinicalOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="opd_audit_events",
    )
    action = models.CharField(max_length=60)
    summary = models.CharField(max_length=255)
    details = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if self.pk:
            raise ValidationError("OPD audit events cannot be changed after creation.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        raise ValidationError("OPD audit events cannot be deleted.")

    def __str__(self):
        return f"{self.action}: {self.patient.uhid}"


class OPDReferral(models.Model):
    class ReferralType(models.TextChoices):
        INTERNAL = "internal", "Internal cross-consultation"
        EXTERNAL = "external", "External referral"

    class Urgency(models.TextChoices):
        ROUTINE = "routine", "Routine"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent to destination queue"
        ACCEPTED = "accepted", "Accepted"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="opd_referrals")
    consultation = models.ForeignKey(
        ConsultationSession, on_delete=models.SET_NULL, null=True, blank=True, related_name="opd_referrals"
    )
    referral_type = models.CharField(max_length=12, choices=ReferralType.choices, default=ReferralType.INTERNAL)
    department = models.CharField(max_length=100)
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="opd_referrals_received",
    )
    reason = models.CharField(max_length=255)
    urgency = models.CharField(max_length=10, choices=Urgency.choices, default=Urgency.ROUTINE)
    notes = models.TextField(blank=True)
    attachment = models.FileField(upload_to="opd_referrals/", blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="opd_referrals_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.patient.uhid} · {self.department} referral"


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
    order = models.ForeignKey(ClinicalOrder, on_delete=models.PROTECT, null=True, blank=True, related_name="reports")
    summary = models.TextField(blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="published_service_reports")
    published_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_service_reports")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    attachment = models.FileField(storage=private_report_storage, upload_to=report_upload_path, blank=True)
    status = models.CharField(max_length=30, default="Completed", verbose_name="Status")
    details = models.JSONField(default=dict, blank=True, verbose_name="Report Details")
    report_date = models.DateTimeField(auto_now_add=True, verbose_name="Report Date")

    class Meta:
        ordering = ["-report_date"]
        verbose_name = "Diagnostic Report"
        verbose_name_plural = "Diagnostic Reports"

    def __str__(self):
        return f"{self.title} — {self.patient.full_name} ({self.category})"



class NurseQueueTicket(models.Model):
    """Triage ticket that moves a visit between the OPD Desk and the Nurse Desk.

    Flow: OPD Desk → *waiting* (nurse queue) → *in_assessment* → *completed*
    (returned to the OPD Desk for doctor allocation). "recalled" keeps the
    ticket alive for a repeat vitals round without the visit returning to the
    OPD Desk.
    """

    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting in nurse queue"
        IN_ASSESSMENT = "in_assessment", "In assessment"
        COMPLETED = "completed", "Assessed · back at OPD Desk"
        RECALLED = "recalled", "Recalled for re-assessment"
        CANCELLED = "cancelled", "Cancelled"

    class Priority(models.TextChoices):
        ROUTINE = "routine", "Routine"
        URGENT = "urgent", "Urgent"
        EMERGENCY = "emergency", "Emergency"

    class Source(models.TextChoices):
        OPD_DESK = "opd_desk", "OPD Desk allocation"
        REGISTRATION = "registration", "Registration intake"
        EMERGENCY = "emergency", "Emergency intake"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="nurse_tickets",
        verbose_name="Patient",
    )
    appointment = models.ForeignKey(
        "appointments.Appointment", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nurse_tickets",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.WAITING, db_index=True,
    )
    priority = models.CharField(
        max_length=12, choices=Priority.choices, default=Priority.ROUTINE, db_index=True,
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.OPD_DESK)
    visit_note = models.CharField(
        max_length=255, blank=True, default="",
        verbose_name="Allocation note", help_text="Reason / instructions from the OPD Desk.",
    )

    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nurse_tickets_sent",
    )
    sent_at = models.DateTimeField(auto_now_add=True)
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nurse_tickets_claimed",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    #: Minutes the nurse estimates before the visit is ready for the OPD Desk.
    eta_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nurse_tickets_completed",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Returned to OPD Desk at",
    )
    assessment = models.ForeignKey(
        InitialAssessment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tickets",
    )
    escalated = models.BooleanField(default=False, verbose_name="Escalated to doctor / emergency")
    response_notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-sent_at"]
        verbose_name = "Nurse Queue Ticket"
        verbose_name_plural = "Nurse Queue Tickets"
        indexes = [models.Index(fields=["status", "priority"])]

    def __str__(self):
        return f"NQ-{self.pk or 'new'} · {self.patient.full_name} ({self.get_status_display()})"

    @property
    def ticket_number(self):
        return f"NQ-{self.pk:05d}" if self.pk else "NQ-NEW"

    @property
    def is_open(self):
        return self.status in (
            self.Status.WAITING, self.Status.IN_ASSESSMENT, self.Status.RECALLED,
        )

    @property
    def wait_minutes(self):
        """Minutes since the OPD Desk sent this visit (stops when assessment starts)."""
        from django.utils import timezone
        end = self.claimed_at or timezone.now()
        if not self.sent_at:
            return 0
        return max(int((end - self.sent_at).total_seconds() // 60), 0)


class NurseTask(models.Model):
    """Nursing to-do item (dressing, injection, monitoring, BP check …)."""

    class Kind(models.TextChoices):
        VITALS = "vitals", "Vitals check"
        DRESSING = "dressing", "Dressing / wound care"
        INJECTION = "injection", "Injection"
        MEDICATION = "medication", "Medication administration"
        MONITORING = "monitoring", "Monitoring round"
        SAMPLE = "sample", "Sample collection"
        OTHER = "other", "Other nursing task"

    class Priority(models.TextChoices):
        ROUTINE = "routine", "Routine"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"
        SKIPPED = "cancelled", "Cancelled"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="nurse_tasks", verbose_name="Patient",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.VITALS)
    title = models.CharField(max_length=255)
    notes = models.TextField(blank=True, default="")
    outcome = models.CharField(max_length=255, blank=True, default="")
    priority = models.CharField(
        max_length=12, choices=Priority.choices, default=Priority.ROUTINE,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    due_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nurse_tasks_assigned",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nurse_tasks_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="nurse_tasks_completed",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Source(models.TextChoices):
        MANUAL = "manual", "Care board"
        TRIAGE = "triage", "Triage follow-up"
        DOCTOR = "doctor_order", "Doctor order"
        LAB = "lab_collect", "Sample collection"

    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    ticket = models.ForeignKey(
        "NurseQueueTicket", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks",
    )
    consultation = models.ForeignKey(
        "ConsultationSession", on_delete=models.SET_NULL, null=True, blank=True, related_name="nurse_tasks",
    )
    order = models.ForeignKey(
        "ClinicalOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="nurse_tasks",
    )

    class Meta:
        ordering = ["status", "due_at", "-created_at"]
        verbose_name = "Nurse Task"
        verbose_name_plural = "Nurse Tasks"

    def __str__(self):
        return f"{self.title} — {self.patient.full_name} ({self.get_status_display()})"

    @property
    def is_overdue(self):
        from django.utils import timezone
        return bool(
            self.due_at
            and self.status in (self.Status.PENDING, self.Status.IN_PROGRESS)
            and self.due_at < timezone.now()
        )


class ServiceDepartment(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ServiceDefinition(models.Model):
    class Modality(models.TextChoices):
        NONE = "", "Not applicable"
        XRAY = "xray", "X-Ray"
        USG = "usg", "Ultrasound (USG)"

    name = models.CharField(max_length=255, unique=True)
    department = models.ForeignKey(ServiceDepartment, on_delete=models.PROTECT, related_name="services")
    modality = models.CharField(
        max_length=10,
        choices=Modality.choices,
        blank=True,
        default=Modality.NONE,
        help_text="Imaging modality for radiology services. Other departments leave this as not applicable.",
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="Price (₹)",
        help_text="Amount charged at the registration desk for this service.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} · {self.department.name}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.price is not None and self.price < Decimal("0"):
            raise ValidationError({"price": "Price cannot be negative."})


class Medicine(models.Model):
    name = models.CharField(max_length=255, unique=True)
    generic_name = models.CharField(max_length=255, blank=True)
    strength = models.CharField(max_length=100, blank=True)
    form = models.CharField(max_length=50, default="Tablet")
    unit = models.CharField(max_length=30, choices=[("tablet", "Tablet"), ("capsule", "Capsule"), ("bottle", "Bottle"), ("tube", "Tube"), ("vial", "Vial"), ("unit", "Unit")], default="tablet")
    pack_size = models.PositiveIntegerField(default=1)
    reorder_level = models.PositiveIntegerField(default=10)
    is_active = models.BooleanField(default=True)

    manufacturer = models.CharField(max_length=200, blank=True)
    barcode = models.CharField(max_length=100, blank=True)
    photo = models.ImageField(upload_to="medicine_photos/", blank=True)
    photo_source = models.URLField(max_length=2000, blank=True)
    photo_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="medicine_photos_approved")
    photo_approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.CheckConstraint(condition=models.Q(pack_size__gt=0), name="medicine_pack_positive")]

    def __str__(self):
        return self.name

    @property
    def available_quantity(self):
        from django.utils import timezone
        from django.db.models import Sum, F
        return self.batches.filter(expiry_date__gte=timezone.localdate()).aggregate(n=Sum(F("on_hand") - F("reserved")))["n"] or 0


class MedicineBatch(models.Model):
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT, related_name="batches")
    batch_number = models.CharField(max_length=100)
    expiry_date = models.DateField()
    supplier = models.CharField(max_length=200)
    invoice_reference = models.CharField(max_length=100)
    purchase_cost = models.DecimalField(max_digits=10, decimal_places=2)
    selling_price = models.DecimalField(max_digits=10, decimal_places=2)
    on_hand = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["expiry_date", "id"]
        constraints = [
            models.UniqueConstraint(fields=["medicine", "batch_number"], name="medicine_batch_unique"),
            models.CheckConstraint(condition=models.Q(reserved__lte=models.F("on_hand")), name="batch_reservation_within_stock"),
            models.CheckConstraint(condition=models.Q(purchase_cost__gte=0, selling_price__gte=0), name="batch_prices_nonnegative"),
        ]

    @property
    def available(self):
        return self.on_hand - self.reserved

    def __str__(self):
        return f"{self.medicine.name} · {self.batch_number}"


class PharmacyIssue(models.Model):
    class Status(models.TextChoices):
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
        DISPENSED = "dispensed", "Dispensed"
        CANCELLED = "cancelled", "Cancelled"

    consultation = models.ForeignKey(ConsultationSession, on_delete=models.PROTECT, related_name="pharmacy_issues")
    bill = models.OneToOneField(PatientBill, on_delete=models.PROTECT, related_name="pharmacy_issue")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AWAITING_PAYMENT)
    request_key = models.UUIDField(unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="pharmacy_issues_created")
    dispensed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="pharmacy_issues_handed_over")
    created_at = models.DateTimeField(auto_now_add=True)
    dispensed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def stale(self):
        from datetime import timedelta
        from django.utils import timezone
        return self.status == "awaiting_payment" and self.bill.status == "pending" and self.created_at < timezone.now() - timedelta(hours=24)

    @property
    def ready(self):
        return self.status == "awaiting_payment" and self.bill.status == "paid"


class PharmacyIssueLine(models.Model):
    issue = models.ForeignKey(PharmacyIssue, on_delete=models.PROTECT, related_name="lines")
    prescription = models.ForeignKey(PrescriptionItem, on_delete=models.PROTECT, related_name="issue_lines")
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    medicine_name = models.CharField(max_length=255)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0, unit_price__gte=0), name="issue_line_values_positive")]

    @property
    def total(self):
        return self.quantity * self.unit_price


class BatchAllocation(models.Model):
    line = models.ForeignKey(PharmacyIssueLine, on_delete=models.PROTECT, related_name="allocations")
    batch = models.ForeignKey(MedicineBatch, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0), name="allocation_quantity_positive")]


class ImmutableLedgerQuerySet(models.QuerySet):
    def update(self, **kwargs):
        from django.core.exceptions import ValidationError
        raise ValidationError("Stock movements are append-only; create a correcting adjustment.")

    def delete(self):
        from django.core.exceptions import ValidationError
        raise ValidationError("Stock movements cannot be deleted.")


class StockMovement(models.Model):
    batch = models.ForeignKey(MedicineBatch, on_delete=models.PROTECT, related_name="movements")
    quantity = models.IntegerField()
    kind = models.CharField(max_length=12, choices=[("receipt", "Receipt"), ("issue", "Issue"), ("adjustment", "Adjustment")])
    reason = models.CharField(max_length=255)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    issue = models.ForeignKey(PharmacyIssue, on_delete=models.PROTECT, null=True, blank=True)
    request_key = models.UUIDField(unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = ImmutableLedgerQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if self.pk:
            raise ValidationError("Stock movements are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        raise ValidationError("Stock movements cannot be deleted.")


class MedicineImport(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="medicine_imports")
    source = models.FileField(storage=private_report_storage, upload_to=pharmacy_import_path)
    filename = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=12, choices=[("catalogue", "Medicines only"), ("stock", "Medicines and stock")])
    columns = models.JSONField(default=list)
    raw_rows = models.JSONField(default=list)
    mapping = models.JSONField(default=dict)
    rows = models.JSONField(default=list)
    result = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class MedicineImportReceipt(models.Model):
    draft = models.ForeignKey(MedicineImport, on_delete=models.PROTECT, related_name="receipts")
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT)
    row_number = models.PositiveIntegerField()
    signature = models.CharField(max_length=64, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["draft", "row_number"], name="medicine_import_receipt_row_unique"),
        ]
