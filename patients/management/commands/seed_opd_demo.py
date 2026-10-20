from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import UserProfile
from patients.models import (
    ClinicalOrder,
    ConsultationSession,
    InitialAssessment,
    Patient,
    PatientBill,
    PrescriptionItem,
)


class Command(BaseCommand):
    help = "Create synthetic OPD records for local demonstrations. No login credentials are created."

    def handle(self, *args, **options):
        now = timezone.now()
        User = get_user_model()
        doctors = []
        for username, first_name, specialty in (
            ("demo_opd_general", "Demo General", "GEN"),
            ("demo_opd_cardiology", "Demo Cardiology", "CAR"),
        ):
            doctor, created = User.objects.get_or_create(
                username=username,
                defaults={"first_name": first_name, "email": f"{username}@example.invalid"},
            )
            if created:
                doctor.set_unusable_password()
                doctor.save(update_fields=["password"])
            doctor.profile.designation = UserProfile.Designation.DOCTOR
            doctor.profile.specialty = specialty
            doctor.profile.save(update_fields=["designation", "specialty"])
            doctors.append(doctor)

        records = [
            ("DEMO-OPD-001", "Asha", "Rao", 34, "F", True, "Penicillin"),
            ("DEMO-OPD-002", "Kabir", "Sen", 51, "M", False, ""),
            ("DEMO-OPD-003", "Mira", "Das", 27, "F", False, ""),
            ("DEMO-OPD-004", "Rohan", "Khan", 63, "M", False, "Sulfa"),
            ("DEMO-OPD-005", "Nila", "Roy", 42, "F", False, ""),
            ("DEMO-OPD-006", "Arun", "Iyer", 38, "M", False, ""),
            ("DEMO-OPD-007", "Tara", "Bose", 46, "F", False, "Latex"),
            ("DEMO-OPD-008", "Dev", "Patel", 19, "M", False, ""),
            ("DEMO-OPD-009", "Ira", "Nair", 32, "F", False, ""),
        ]
        patients = []
        for index, (mr_number, first, last, age, gender, emergency, allergies) in enumerate(records):
            patient, _ = Patient.objects.update_or_create(
                mr_number=mr_number,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "date_of_birth": timezone.localdate().replace(year=timezone.localdate().year - age),
                    "gender": gender,
                    "phone": f"9000000{index + 101:03d}",
                    "allergies": allergies,
                    "is_emergency": emergency,
                    "is_demo": True,
                    "assigned_doctor": doctors[index % len(doctors)] if index in (3, 5, 6) else None,
                },
            )
            Patient.objects.filter(pk=patient.pk).update(created_at=now - timedelta(minutes=12 * index))
            patients.append(patient)

        InitialAssessment.objects.update_or_create(
            patient=patients[1],
            defaults={"status": InitialAssessment.Status.DRAFT, "complaints": "Headache (2 days)", "pulse": 78},
        )
        for index in (2, 3, 4, 5, 6, 7, 8):
            InitialAssessment.objects.update_or_create(
                patient=patients[index],
                defaults={
                    "status": InitialAssessment.Status.COMPLETED,
                    "complaints": "Demo symptom record",
                    "temperature": 37.0,
                    "pulse": 76 + index,
                    "bp_systolic": 118,
                    "bp_diastolic": 76,
                    "spo2": 98,
                },
            )

        consultation_specs = {
            3: ConsultationSession.Status.IN_PROGRESS,
            4: ConsultationSession.Status.IN_PROGRESS,
            5: ConsultationSession.Status.IN_PROGRESS,
            6: ConsultationSession.Status.IN_PROGRESS,
            7: ConsultationSession.Status.COMPLETED,
            8: ConsultationSession.Status.IN_PROGRESS,
        }
        consultations = {}
        for index, status in consultation_specs.items():
            consultations[index] = ConsultationSession.objects.filter(
                patient=patients[index]
            ).order_by("-created_at").first()
            if consultations[index] is None:
                consultations[index] = ConsultationSession(patient=patients[index])
            consultations[index].doctor = patients[index].assigned_doctor or doctors[index % len(doctors)]
            consultations[index].visit_token = f"DEMO-VISIT-{index + 1:03d}"
            consultations[index].status = status
            consultations[index].clinical_note = "Synthetic demonstration encounter."
            consultations[index].save()

        for index, order_type, name, status in (
            (4, "radiology", "Chest X-Ray · Demo", "In Progress"),
            (5, "laboratory", "Basic Metabolic Panel · Demo", "Review Required"),
            (6, "other", "Procedure Room consent checklist · Demo", "Ordered"),
            (6, "other", "Physiotherapy session · Demo", "Ordered"),
            (6, "other", "Counselling session · Demo", "Ordered"),
            (6, "other", "ECG acquisition · Demo", "Ordered"),
        ):
            ClinicalOrder.objects.update_or_create(
                consultation=consultations[index], name=name,
                defaults={"order_type": order_type, "status": status},
            )
        ClinicalOrder.objects.filter(
            consultation=consultations[3], name="Complete Blood Count · Demo"
        ).update(status="Cancelled")
        PrescriptionItem.objects.update_or_create(
            consultation=consultations[3], medicine_name="Demo medication - not for treatment",
            defaults={"dose": "1 tablet", "route": "Oral", "frequency": "Once daily", "duration": "3 days"},
        )
        PatientBill.objects.update_or_create(
            patient=patients[3], source=PatientBill.Source.DOCTOR, description="Demo pending bill",
            defaults={"amount": "100.00", "status": PatientBill.Status.PENDING},
        )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(patients)} synthetic OPD patients and workflow records. No credentials created."
        ))