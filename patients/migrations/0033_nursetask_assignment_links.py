from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("patients", "0032_clinicalorder_sessions_planned_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="nursetask",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "Care board"),
                    ("triage", "Triage follow-up"),
                    ("doctor_order", "Doctor order"),
                    ("lab_collect", "Sample collection"),
                ],
                default="manual",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="nursetask",
            name="consultation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="nurse_tasks",
                to="patients.consultationsession",
            ),
        ),
        migrations.AddField(
            model_name="nursetask",
            name="order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="nurse_tasks",
                to="patients.clinicalorder",
            ),
        ),
        migrations.AddField(
            model_name="nursetask",
            name="ticket",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tasks",
                to="patients.nursequeueticket",
            ),
        ),
    ]
