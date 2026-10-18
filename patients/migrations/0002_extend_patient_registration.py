from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("patients", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="second_name",
            field=models.CharField(max_length=100, blank=True, verbose_name="Second Name"),
        ),
        migrations.AddField(
            model_name="patient",
            name="marital_status",
            field=models.CharField(
                max_length=10,
                blank=True,
                choices=[
                    ("single", "Single"),
                    ("married", "Married"),
                    ("widowed", "Widowed"),
                    ("divorced", "Divorced"),
                ],
                verbose_name="Marital Status",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="aadhar_no",
            field=models.CharField(max_length=20, blank=True, verbose_name="Aadhar No."),
        ),
        migrations.AddField(
            model_name="patient",
            name="abha_no",
            field=models.CharField(max_length=30, blank=True, verbose_name="ABHA No"),
        ),
        migrations.AddField(
            model_name="patient",
            name="care_of",
            field=models.CharField(max_length=150, blank=True, verbose_name="C/O"),
        ),
        migrations.AddField(
            model_name="patient",
            name="place_panch",
            field=models.CharField(max_length=150, blank=True, verbose_name="Place/Panch."),
        ),
        migrations.AddField(
            model_name="patient",
            name="nationality",
            field=models.CharField(max_length=60, blank=True, default="India", verbose_name="Nationality"),
        ),
        migrations.AddField(
            model_name="patient",
            name="state",
            field=models.CharField(max_length=80, blank=True, verbose_name="State"),
        ),
        migrations.AddField(
            model_name="patient",
            name="district",
            field=models.CharField(max_length=80, blank=True, verbose_name="District"),
        ),
        migrations.AddField(
            model_name="patient",
            name="pin_code",
            field=models.CharField(max_length=10, blank=True, verbose_name="Pin Code"),
        ),
        migrations.AddField(
            model_name="patient",
            name="zone",
            field=models.CharField(max_length=60, blank=True, verbose_name="Zone"),
        ),
        migrations.AddField(
            model_name="patient",
            name="block",
            field=models.CharField(max_length=60, blank=True, verbose_name="Block"),
        ),
        migrations.AddField(
            model_name="patient",
            name="religion",
            field=models.CharField(max_length=60, blank=True, verbose_name="Religion"),
        ),
        migrations.AddField(
            model_name="patient",
            name="reg_fee",
            field=models.DecimalField(max_digits=8, decimal_places=2, default=50, verbose_name="Reg Fee"),
        ),
        migrations.AddField(
            model_name="patient",
            name="con_fee",
            field=models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name="Con Fee"),
        ),
        migrations.AddField(
            model_name="patient",
            name="discount",
            field=models.DecimalField(max_digits=8, decimal_places=2, default=0, verbose_name="Discount"),
        ),
        migrations.AddField(
            model_name="patient",
            name="payment_type",
            field=models.CharField(
                max_length=10,
                blank=True,
                default="cash",
                choices=[
                    ("cash", "Cash"),
                    ("credit", "Credit"),
                    ("free", "Free"),
                    ("card", "Card"),
                    ("part", "Part"),
                ],
                verbose_name="Payment Type",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="patient_category",
            field=models.CharField(
                max_length=20,
                blank=True,
                default="GENERAL",
                choices=[
                    ("GENERAL", "General"),
                    ("BPL", "BPL"),
                    ("ESI", "ESI"),
                    ("GOVT", "Govt"),
                    ("VIP", "VIP"),
                ],
                verbose_name="Patient Category",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="reference_no",
            field=models.CharField(max_length=40, blank=True, verbose_name="Reference No."),
        ),
        migrations.AddField(
            model_name="patient",
            name="valid_upto",
            field=models.DateField(null=True, blank=True, verbose_name="Valid Upto"),
        ),
        migrations.AddField(
            model_name="patient",
            name="billing_type",
            field=models.CharField(
                max_length=20,
                blank=True,
                default="Individual",
                choices=[
                    ("Individual", "Individual"),
                    ("Corporate", "Corporate"),
                    ("Insurance", "Insurance"),
                ],
                verbose_name="Billing Type",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="mlc",
            field=models.BooleanField(default=False, verbose_name="MLC Regn."),
        ),
        migrations.AddField(
            model_name="patient",
            name="mr_number",
            field=models.CharField(max_length=20, blank=True, verbose_name="MR Number"),
        ),
    ]
