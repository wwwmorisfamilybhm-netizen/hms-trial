import json
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from core.models import UserProfile
from patients.models import Patient, ConsultationSession, PrescriptionItem

User = get_user_model()


class DoctorConsultationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.doctor_user = User.objects.create_user(
            username="dr_rao", password="password123", first_name="Rao"
        )
        profile, _ = UserProfile.objects.get_or_create(user=self.doctor_user)
        profile.designation = UserProfile.Designation.DOCTOR
        profile.specialty = "GEN"
        profile.save()
        self.client.login(username="dr_rao", password="password123")

        self.patient = Patient.objects.create(
            first_name="Anita",
            last_name="Sharma",
            phone="9876543210",
            gender=Patient.Gender.FEMALE,
            allergies="Penicillin, Sulfa",
        )

    def test_doctor_consultation_picker_view(self):
        """Test consultation workstation renders patient picker when no patient is selected."""
        url = reverse("doctor_consultation")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select Patient for Consultation")
        self.assertContains(response, "Anita Sharma")

    def test_doctor_consultation_patient_workspace(self):
        """Test consultation workstation renders 7-tab interface for selected patient."""
        url = reverse("doctor_consultation") + f"?patient={self.patient.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Anita Sharma")
        self.assertContains(response, "1. Assessment")
        self.assertContains(response, "6. Prescription")
        self.assertContains(response, "Hospital Formulary")

    def test_drug_search_api(self):
        """Test drug search endpoint returns filtered formulary items."""
        url = reverse("drug_search_api") + "?q=paracetamol"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(any("Paracetamol" in d["name"] for d in data.get("drugs", [])))

    def test_icd10_search_api(self):
        """Test ICD-10 search endpoint."""
        url = reverse("icd10_search_api") + "?q=J06"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(any(r["code"] == "J06.9" for r in data.get("results", [])))

    def test_safety_check_api_allergy_warning(self):
        """Test safety check flags allergy alert when penicillin-derivative is prescribed."""
        url = reverse("safety_check_api")
        payload = {
            "patient_id": self.patient.pk,
            "medicines": ["Amoxicillin-Clavulanate 625 mg Tablet"],
        }
        response = self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("has_alerts"))
        self.assertTrue(any("Penicillin" in a for a in data.get("allergy_alerts", [])))

    def test_save_consultation_api(self):
        """Test saving a consultation draft with prescription items."""
        url = reverse("save_consultation")
        payload = {
            "patient_id": self.patient.pk,
            "action": "save_draft",
            "clinical_note": "Patient presents with cough and fever.",
            "diagnosis_primary": "Acute Upper Respiratory Infection",
            "diagnosis_primary_icd": "J06.9",
            "prescriptions": [
                {
                    "medicine_name": "Paracetamol 500 mg Tablet",
                    "dose": "500 mg",
                    "route": "Oral",
                    "frequency": "TDS",
                    "duration": "5 days",
                    "instructions": "For fever",
                }
            ],
            "orders": [
                {
                    "order_type": "laboratory",
                    "name": "Complete Blood Count (CBC)",
                    "status": "Ordered",
                }
            ],
        }
        response = self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("status"), ConsultationSession.Status.DRAFT)

        consultation = ConsultationSession.objects.get(pk=data.get("id"))
        self.assertEqual(consultation.diagnosis_primary_icd, "J06.9")
        self.assertEqual(consultation.prescriptions.count(), 1)
        self.assertEqual(consultation.orders.count(), 1)
