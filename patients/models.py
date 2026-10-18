from datetime import date

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
    last_name = models.CharField(max_length=100)
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
    religion = models.CharField(max_length=60, blank=True, verbose_name="Religion")

    # Billing & fees
    reg_fee = models.DecimalField(max_digits=8, decimal_places=2, default=50, verbose_name="Reg Fee")
    con_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name="Con Fee")
    discount = models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name="Discount")

    class PaymentType(models.TextChoices):
        CASH = "cash", "Cash"
        CREDIT = "credit", "Credit"
        FREE = "free", "Free"
        CARD = "card", "Card"
        PART = "part", "Part"

    payment_type = models.CharField(
        max_length=10, blank=True, default="cash", choices=PaymentType.choices, verbose_name="Payment Type"
    )

    class PatientCategory(models.TextChoices):
        GENERAL = "GENERAL", "General"
        BPL = "BPL", "BPL"
        ESI = "ESI", "ESI"
        GOVT = "GOVT", "Govt"
        VIP = "VIP", "VIP"

    patient_category = models.CharField(
        max_length=20, blank=True, default="GENERAL", choices=PatientCategory.choices, verbose_name="Patient Category"
    )
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
    photo = models.FileField(upload_to="patient_photos/", blank=True, null=True, verbose_name="Patient Photo")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

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
