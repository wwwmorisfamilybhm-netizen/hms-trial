from django.db import migrations

DEPARTMENTS = [
    ("laboratory", "Laboratory"), ("radiology", "Radiology · X-Ray & USG"),
    ("cardiology", "Cardiology · ECG"), ("physiotherapy", "Physiotherapy"),
    ("counselling", "Counselling"), ("procedure-room", "Procedure Room"), ("procedures", "Procedures"),
]


def seed(apps, schema_editor):
    Department = apps.get_model("patients", "ServiceDepartment")
    Definition = apps.get_model("patients", "ServiceDefinition")
    Order = apps.get_model("patients", "ClinicalOrder")
    Rx = apps.get_model("patients", "PrescriptionItem")
    departments = {slug: Department.objects.get_or_create(slug=slug, defaults={"name": name})[0] for slug, name in DEPARTMENTS}
    # Only migration-time classification uses historical free-text keywords.
    def classify(name, order_type):
        value = name.casefold()
        matches = []
        for token, slug in [("ecg", "cardiology"), ("echocardiography", "cardiology"), ("physio", "physiotherapy"), ("counsel", "counselling"), ("procedure room", "procedure-room")]:
            if token in value:
                matches.append(slug)
        if len(set(matches)) == 1:
            return matches[0]
        if matches or "pharm" in value:
            return None
        if order_type in ("laboratory", "radiology"):
            return order_type
        if value in ("nebulisation", "iv fluids", "wound dressing", "injection (as advised)"):
            return "procedures"
        return None
    for order in Order.objects.all().iterator():
        slug = classify(order.name, order.order_type)
        if slug:
            definition, _ = Definition.objects.get_or_create(name=order.name, defaults={"department": departments[slug]})
            Order.objects.filter(pk=order.pk).update(department=departments[slug], service_definition=definition)
    from django.db.models import F
    Rx.objects.all().update(legacy_dispensed_quantity=F("dispensed_quantity"))
    # Preserve old outstanding work in the clarification queue; saved drafts stay private.
    Rx.objects.exclude(consultation__status="draft").exclude(dispense_status="cancelled").update(released_at=F("created_at"))
    banks = BANKS
    for kind, _, names in banks:
        for name in names:
            slug = classify(name, kind)
            if slug:
                Definition.objects.get_or_create(name=name, defaults={"department": departments[slug]})


BANKS = [('laboratory', 'Laboratory', ['Complete Blood Count (CBC)', 'C-Reactive Protein (CRP)', 'Erythrocyte Sedimentation Rate (ESR)', 'Liver Function Test (LFT)', 'Renal Function Test (RFT)', 'Blood Sugar (RBS / FBS)', 'HbA1c', 'Serum Electrolytes', 'Urine Routine & Microscopy', 'Urine Culture', 'Dengue NS1 Antigen', 'Thyroid Profile (T3 / T4 / TSH)']), ('radiology', 'Radiology & Imaging', ['Chest X-Ray (PA View)', 'USG Whole Abdomen', 'X-Ray Limb', 'CT Head', 'ECG (12 Lead)', '2D Echocardiography']), ('other', 'Procedures & Services', ['Nebulisation', 'IV Fluids', 'Wound Dressing', 'Injection (as advised)', 'Physiotherapy Referral', 'Counselling Referral']), ('laboratory', 'Laboratory', ['CBC', 'ESR', 'Blood sugar (RBS/FBS)', 'HbA1c', 'RFT', 'LFT', 'Urine routine', 'Urine culture', 'Serum electrolytes', 'CRP', 'Dengue NS1']), ('radiology', 'Radiology', ['Chest X-Ray PA', 'USG Abdomen', 'ECG', '2D Echo', 'X-Ray Limb', 'CT Head']), ('other', 'Nursing / procedures', ['Nebulisation', 'IV fluids', 'Injection (as advised)', 'Dressing', 'Nebuliser refill'])]


class Migration(migrations.Migration):
    dependencies = [("patients", "0025_servicedefinition_servicedepartment_and_more")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
