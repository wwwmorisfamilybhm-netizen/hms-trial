from datetime import date, datetime, timedelta

from django.db import models

from patients.models import Patient


class Doctor(models.Model):
    """A doctor working at the hospital."""

    SPECIALTIES = [
        ("GEN", "General Medicine"),
        ("CAR", "Cardiology"),
        ("DER", "Dermatology"),
        ("PED", "Pediatrics"),
        ("ORT", "Orthopedics"),
        ("GYN", "Gynecology"),
        ("NEU", "Neurology"),
        ("ENT", "ENT"),
        ("OPT", "Ophthalmology"),
        ("PSY", "Psychiatry"),
    ]

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    specialty = models.CharField(max_length=3, choices=SPECIALTIES, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)

    class Meta:
        ordering = ["last_name"]

    def __str__(self):
        return f"Dr. {self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"Dr. {self.first_name} {self.last_name}"

    @property
    def specialty_display(self):
        return self.get_specialty_display() if self.specialty else "—"


class Appointment(models.Model):
    """A scheduled visit by a patient with a doctor."""

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        NO_SHOW = "NO_SHOW", "No-show"

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="appointments"
    )
    doctor = models.ForeignKey(
        Doctor, on_delete=models.PROTECT, related_name="appointments"
    )
    date = models.DateField()
    start_time = models.TimeField()
    duration_minutes = models.PositiveIntegerField(default=30)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SCHEDULED
    )
    reason = models.CharField(
        max_length=255, help_text="Reason for the visit"
    )
    notes = models.TextField(blank=True, help_text="Doctor's notes after the visit")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "start_time"]
        # One doctor cannot be in two rooms at once:
        constraints = [
            models.UniqueConstraint(
                fields=["doctor", "date", "start_time"],
                name="unique_doctor_slot",
            )
        ]

    def __str__(self):
        return f"{self.patient} → {self.doctor} on {self.date} {self.start_time:%H:%M}"

    @property
    def end_time(self):
        start = datetime.combine(self.date, self.start_time)
        return (start + timedelta(minutes=self.duration_minutes)).time()

    @property
    def is_upcoming(self):
        return self.status == self.Status.SCHEDULED and self.date >= date.today()
