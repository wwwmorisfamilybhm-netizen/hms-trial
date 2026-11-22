"""One-time doctor account reset while retaining historical attribution."""

from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import UserProfile
from patients.models import Patient


class Command(BaseCommand):
    help = "Archive doctor identities, clear patient assignments, and delete their login accounts."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true", help="Apply the reset (default is a dry run).")

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        doctors = list(User.objects.select_for_update().filter(
            profile__designation=UserProfile.Designation.DOCTOR
        ).order_by("pk"))
        references = []
        for model in apps.get_models():
            if model is UserProfile:
                continue
            for field in model._meta.local_fields:
                if field.is_relation and field.related_model is User and (field.many_to_one or field.one_to_one):
                    if model is Patient and field.name == "assigned_doctor":
                        continue
                    references.append((model, field))

        for doctor in doctors:
            assigned = Patient.objects.filter(assigned_doctor=doctor).count()
            counts = [(model, field, model._base_manager.filter(**{field.name: doctor}).count())
                      for model, field in references]
            counts = [(model, field, count) for model, field, count in counts if count]
            self.stdout.write(f"Doctor {doctor.pk} ({doctor.username}): {assigned} patient assignments; "
                              + (", ".join(f"{m._meta.label}.{f.name}={n}" for m, f, n in counts) or "no historical references"))
            if not options["execute"]:
                continue
            archive = User.objects.create_user(
                username=f"archived_doctor_{doctor.pk}_{uuid4().hex[:8]}",
                first_name=doctor.first_name or (doctor.username if not doctor.last_name else ""),
                last_name=doctor.last_name,
                is_active=False,
            )
            archive.profile.notes = f"Archived doctor identity; former username: {doctor.username}"[:255]
            archive.profile.save(update_fields=["notes"])
            Patient.objects.filter(assigned_doctor=doctor).update(assigned_doctor=None)
            for model, field, _ in counts:
                model._base_manager.filter(**{field.name: doctor}).update(**{field.name: archive})
            doctor.delete()
        self.stdout.write(f"{'Archived and deleted' if options['execute'] else 'Would archive and delete'} {len(doctors)} doctor accounts.")
