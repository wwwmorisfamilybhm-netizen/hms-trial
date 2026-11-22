import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import UserProfile
from core.views import _medgemma_mock_draft, _triage_priority_score
from patients.models import InitialAssessment, NurseQueueTicket, Patient


class AssessmentTemperatureTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="temperature_opd")
        user.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        user.profile.save()
        self.client.force_login(user)
        self.patient = Patient.objects.create(first_name="Temperature")

    def post_temperature(self, endpoint, temperature, unit="F"):
        return self.client.post(reverse(endpoint), data=json.dumps({
            "patient_id": self.patient.pk, "temperature": temperature,
            "temperature_unit": unit, "action": "draft",
        }), content_type="application/json")

    def test_fahrenheit_round_trip_and_legacy_celsius(self):
        for endpoint in ("save_initial_assessment", "nurse_save_triage"):
            for reading, stored in (("98.6", "37.00"), ("98.7", "37.06"), ("103.1", "39.50")):
                response = self.post_temperature(endpoint, reading)
                self.assertEqual(response.status_code, 200, response.content)
                assessment = InitialAssessment.objects.get(patient=self.patient)
                self.assertEqual(assessment.temperature, Decimal(stored))
                page = self.client.get(reverse("initial_assessment"), {"patient": self.patient.pk})
                self.assertContains(page, f'value="{reading}"')
            self.assertEqual(_triage_priority_score(assessment), 20)
            self.assertEqual(self.post_temperature(endpoint, "37", "F").status_code, 400)
            self.assertEqual(self.post_temperature(endpoint, "98.6", "invalid").status_code, 400)
            self.assertEqual(self.post_temperature(endpoint, "37.2", "C").status_code, 200)
            self.assertEqual(InitialAssessment.objects.get(patient=self.patient).temperature, Decimal("37.20"))

    def test_triage_prefills_fahrenheit_and_ai_uses_correct_unit(self):
        InitialAssessment.objects.create(patient=self.patient, temperature=Decimal("37.00"))
        ticket = NurseQueueTicket.objects.create(patient=self.patient)
        page = self.client.get(reverse("nurse_triage"), {"ticket": ticket.pk})
        self.assertContains(page, 'value="98.6"')
        self.assertContains(page, "Temperature (°F)")
        self.assertIn("98.6°F", _medgemma_mock_draft({"temp": "98.6", "temperature_unit": "F"}, "", []) ["summary"])

    def test_doctor_assessment_shows_fahrenheit_and_retains_alerts(self):
        doctor = get_user_model().objects.create_user(username="temperature_doctor")
        doctor.profile.designation = UserProfile.Designation.DOCTOR
        doctor.profile.save()
        self.client.force_login(doctor)
        assessment = InitialAssessment.objects.create(patient=self.patient, temperature=Decimal("37.00"))
        url = reverse("doctor_consultation")
        page = self.client.get(url, {"patient": self.patient.pk})
        self.assertEqual(page.context["assessment"]["temperature"], "98.6")
        self.assertFalse(page.context["assessment"]["vital_flags"]["temperature"])
        self.assertContains(page, "98.6°F")
        assessment.temperature = Decimal("39.50")
        assessment.save()
        page = self.client.get(url, {"patient": self.patient.pk})
        self.assertEqual(page.context["assessment"]["temperature"], "103.1")
        self.assertTrue(page.context["assessment"]["vital_flags"]["temperature"])
        assessment.temperature = None
        assessment.save()
        page = self.client.get(url, {"patient": self.patient.pk})
        self.assertEqual(page.context["assessment"]["temperature"], "")
