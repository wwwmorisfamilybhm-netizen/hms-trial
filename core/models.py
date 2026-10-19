from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    """Profile extending auth.User to allocate designation and role permissions."""

    class Designation(models.TextChoices):
        REGISTRATION_DESK = "registration_desk", "Registration Desk"
        OPD_ASSISTANT = "opd_assistant", "OPD Assistant"
        DOCTOR = "doctor", "Doctor"
        NURSE = "nurse", "Nurse"
        ADMIN = "admin", "Hospital Administrator"

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
        ("", "Not Specified"),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
        verbose_name="User Account",
    )
    designation = models.CharField(
        max_length=30,
        choices=Designation.choices,
        default=Designation.REGISTRATION_DESK,
        verbose_name="Assigned Designation",
        help_text="Designation assigned to this user. Restricts access to matching dashboards and tools.",
    )
    notes = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Notes / Shift Info",
        help_text="Optional counter name, shift timing, or department notes.",
    )
    specialty = models.CharField(
        max_length=3,
        choices=SPECIALTIES,
        blank=True,
        default="",
        verbose_name="Specialty",
        help_text="Clinical specialty shown under the staff member's name.",
    )
    photo = models.ImageField(
        upload_to="staff_photos/",
        blank=True,
        null=True,
        verbose_name="Profile Photo",
        help_text="Staff portrait shown in the topbar profile menu.",
    )

    class Meta:
        verbose_name = "User Designation Allocation"
        verbose_name_plural = "User Designation Allocations"
        ordering = ["user__username"]

    def __str__(self):
        return f"{self.user.username} — {self.get_designation_display()}"


@receiver(post_save, sender=User)
def create_or_save_user_profile(sender, instance, created, **kwargs):
    """Automatically ensure a UserProfile exists for every User."""
    if created:
        default_desig = (
            UserProfile.Designation.ADMIN
            if instance.is_superuser
            else UserProfile.Designation.REGISTRATION_DESK
        )
        UserProfile.objects.create(user=instance, designation=default_desig)
    else:
        if not hasattr(instance, "profile"):
            default_desig = (
                UserProfile.Designation.ADMIN
                if instance.is_superuser
                else UserProfile.Designation.REGISTRATION_DESK
            )
            UserProfile.objects.create(user=instance, designation=default_desig)
