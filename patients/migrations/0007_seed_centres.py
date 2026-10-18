"""Seed the initial registration centres: CHS and Camp."""
from django.db import migrations


def seed_centres(apps, schema_editor):
    RegistrationOption = apps.get_model("patients", "RegistrationOption")
    for name in ("CHS", "Camp"):
        RegistrationOption.objects.get_or_create(option_type="centre", name=name)


def unseed_centres(apps, schema_editor):
    RegistrationOption = apps.get_model("patients", "RegistrationOption")
    RegistrationOption.objects.filter(
        option_type="centre", name__in=("CHS", "Camp")
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0006_patient_centre_alter_registrationoption_option_type"),
    ]

    operations = [
        migrations.RunPython(seed_centres, unseed_centres),
    ]
