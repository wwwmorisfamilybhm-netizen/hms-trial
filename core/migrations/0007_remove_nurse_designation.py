# Remove the standalone Nurse designation — merged into OPD Assistant.
# Profiles still storing 'nurse' keep working: views map the legacy value to
# opd_assistant until an administrator edits the profile manually.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_userprofile_service_departments'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='designation',
            field=models.CharField(choices=[('registration_desk', 'Registration Desk'), ('opd_assistant', 'OPD Assistant'), ('doctor', 'Doctor'), ('service_staff', 'Service Staff'), ('pharmacy', 'Pharmacy'), ('admin', 'Hospital Administrator')], default='registration_desk', help_text='Designation assigned to this user. Restricts access to matching dashboards and tools.', max_length=30, verbose_name='Assigned Designation'),
        ),
    ]
