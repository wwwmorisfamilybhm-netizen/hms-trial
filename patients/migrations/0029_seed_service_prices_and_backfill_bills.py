from decimal import Decimal

from django.db import migrations


# Placeholder catalogue prices for the seeded OPD services. The service
# catalogue screen can change these at any time; existing bills keep the
# price snapshot recorded when the order was created.
PRICE_MAP = {
    "complete blood count (cbc)": "300",
    "cbc": "300",
    "c-reactive protein (crp)": "400",
    "crp": "400",
    "erythrocyte sedimentation rate (esr)": "150",
    "esr": "150",
    "liver function test (lft)": "600",
    "lft": "600",
    "renal function test (rft)": "600",
    "rft": "600",
    "blood sugar (rbs / fbs)": "100",
    "blood sugar (rbs/fbs)": "100",
    "hba1c": "500",
    "serum electrolytes": "400",
    "urine routine & microscopy": "150",
    "urine routine": "150",
    "urine culture": "500",
    "dengue ns1 antigen": "600",
    "dengue ns1": "600",
    "thyroid profile (t3 / t4 / tsh)": "700",
    "chest x-ray (pa view)": "500",
    "chest x-ray pa": "500",
    "usg whole abdomen": "1200",
    "usg abdomen": "1200",
    "x-ray limb": "400",
    "ct head": "3500",
    "ecg (12 lead)": "300",
    "ecg": "300",
    "2d echocardiography": "1500",
    "2d echo": "1500",
    "nebulisation": "200",
    "nebuliser refill": "200",
    "iv fluids": "300",
    "wound dressing": "250",
    "dressing": "250",
    "injection (as advised)": "100",
    "physiotherapy referral": "500",
    "counselling referral": "500",
}


def seed_prices(apps, schema_editor):
    ServiceDefinition = apps.get_model("patients", "ServiceDefinition")
    ClinicalOrder = apps.get_model("patients", "ClinicalOrder")
    PatientBill = apps.get_model("patients", "PatientBill")
    PharmacyIssue = apps.get_model("patients", "PharmacyIssue")
    Appointment = apps.get_model("appointments", "Appointment")

    # Seed catalogue prices where they have not been configured yet.
    for definition in ServiceDefinition.objects.all().iterator():
        price = PRICE_MAP.get(definition.name.strip().casefold())
        if price is not None and (definition.price is None or definition.price == 0):
            ServiceDefinition.objects.filter(pk=definition.pk).update(
                price=Decimal(price)
            )

    # Snapshot the current catalogue price onto legacy orders that have none.
    definitions = {
        d.name.casefold(): d.price
        for d in ServiceDefinition.objects.all().only("name", "price")
    }
    for order in ClinicalOrder.objects.filter(price=0).iterator():
        price = definitions.get(order.name.strip().casefold())
        if price:
            ClinicalOrder.objects.filter(pk=order.pk).update(price=price)

    # Pharmacy bills already know their consultation through the issue.
    for issue in PharmacyIssue.objects.select_related("bill").iterator():
        if issue.bill_id and issue.consultation_id:
            PatientBill.objects.filter(
                pk=issue.bill_id, consultation__isnull=True
            ).update(consultation_id=issue.consultation_id)

    # Best-effort appointment backfill for pre-visit bills. New bills are
    # linked explicitly at creation time; this only helps historical rows.
    for bill in PatientBill.objects.filter(
        appointment__isnull=True,
        source__in=["registration", "appointment"],
    ).iterator():
        candidates = Appointment.objects.filter(patient_id=bill.patient_id)
        if bill.source == "appointment":
            candidates = candidates.filter(date=bill.created_at.date())
        else:
            candidates = candidates.filter(created_at__date=bill.created_at.date())
        appointment = candidates.order_by("-created_at").first()
        if appointment:
            PatientBill.objects.filter(pk=bill.pk).update(
                appointment_id=appointment.pk
            )


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0002_alter_appointment_doctor"),
        ("patients", "0028_service_pricing_and_billing_links"),
    ]

    operations = [
        migrations.RunPython(seed_prices, migrations.RunPython.noop),
    ]
