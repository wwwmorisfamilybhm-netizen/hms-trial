from io import StringIO
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import UserProfile
from core.views import _opd_patient_flow
from patients.models import ConsultationSession, InitialAssessment, Patient, PatientBill, PharmacyIssue


class DoctorAllocationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.opd = User.objects.create_user(username="allocation_opd")
        self.opd.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        self.opd.profile.save()
        self.doctor = User.objects.create_user(username="active_doctor", first_name="Active")
        self.doctor.profile.designation = UserProfile.Designation.DOCTOR
        self.doctor.profile.save()
        self.inactive = User.objects.create_user(username="inactive_doctor", is_active=False)
        self.inactive.profile.designation = UserProfile.Designation.DOCTOR
        self.inactive.profile.save()
        self.patient = Patient.objects.create(first_name="Queue", last_name="Patient")
        InitialAssessment.objects.create(patient=self.patient, status=InitialAssessment.Status.COMPLETED,
                                         completed_at=timezone.now(), complaints="Test")
        self.client.force_login(self.opd)

    def test_lists_and_assignment_only_accept_active_doctors(self):
        for name in ("opd_assistant_dashboard", "opd_patient_queue"):
            response = self.client.get(reverse(name), {"patient": self.patient.pk})
            self.assertContains(response, "Active")
            self.assertNotContains(response, "inactive_doctor")
        url = reverse("assign_doctor", args=[self.patient.pk])
        for doctor_id in (self.inactive.pk, self.opd.pk, "invalid"):
            response = self.client.post(url, {"doctor_id": doctor_id}, follow=True)
            self.assertContains(response, "Select an active doctor account.")
            self.patient.refresh_from_db()
            self.assertIsNone(self.patient.assigned_doctor)
        self.client.post(url, {"doctor_id": self.doctor.pk})
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor, self.doctor)

    def test_reset_preserves_historical_and_protected_references(self):
        self.patient.assigned_doctor = self.doctor
        self.patient.save(update_fields=["assigned_doctor"])
        consultation = ConsultationSession.objects.create(patient=self.patient, doctor=self.doctor)
        bill = PatientBill.objects.create(patient=self.patient, consultation=consultation, amount=10)
        issue = PharmacyIssue.objects.create(consultation=consultation, bill=bill, created_by=self.doctor,
                                             request_key=uuid4())
        original_id = self.doctor.pk
        call_command("archive_doctor_accounts", stdout=StringIO())
        self.assertTrue(get_user_model().objects.filter(pk=original_id).exists())
        call_command("archive_doctor_accounts", execute=True, stdout=StringIO())
        self.assertFalse(get_user_model().objects.filter(pk=original_id).exists())
        self.patient.refresh_from_db()
        consultation.refresh_from_db()
        issue.refresh_from_db()
        self.assertIsNone(self.patient.assigned_doctor)
        self.assertEqual(issue.created_by_id, consultation.doctor_id)
        self.assertFalse(consultation.doctor.is_active)
        self.assertFalse(consultation.doctor.has_usable_password())
        self.assertEqual(consultation.doctor.first_name, "Active")
        self.assertIsNone(_opd_patient_flow([self.patient])[0]["doctor"])

    def test_assignment_updates_opd_and_doctor_queues_and_can_be_reassigned(self):
        url = reverse("assign_doctor", args=[self.patient.pk])
        pending = self.client.get(reverse("opd_assistant_dashboard"))
        self.assertEqual(pending.context["counts"]["doctor_allocation"], 1)
        self.client.post(url, {"doctor_id": self.doctor.pk})

        dashboard = self.client.get(reverse("opd_assistant_dashboard"))
        self.assertEqual(dashboard.context["counts"]["doctor_allocation"], 0)
        self.assertEqual(dashboard.context["counts"]["triage_allocation"], 0)
        self.assertContains(dashboard, "Doctor Assigned")
        self.assertContains(dashboard, "Dr. Active")
        queue = self.client.get(reverse("opd_patient_queue"), {"patient": self.patient.pk})
        self.assertEqual(queue.context["selected"]["status"], "Doctor Assigned")
        self.assertFalse(queue.context["selected"]["needs_doctor"])
        self.assertEqual(queue.context["allocation_counts"]["doctor"], 0)
        self.assertEqual(queue.context["allocation_counts"]["nurse"], 0)
        self.assertContains(queue, "Doctor assigned — waiting to start consultation")
        self.assertContains(queue, "Reassign doctor")

        self.client.force_login(self.doctor)
        board = self.client.get(reverse("patient_queue"))
        self.assertContains(board, "Queue Patient")
        self.assertTrue(any(row["patient"].pk == self.patient.pk for row in board.context["board_columns"][1]["rows"]))
        listed = self.client.get(reverse("patient_queue"), {"view": "list"})
        self.assertContains(listed, "Start →")

        self.inactive.is_active = True
        self.inactive.save(update_fields=["is_active"])
        self.client.force_login(self.opd)
        self.client.post(url, {"doctor_id": self.inactive.pk})
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor_id, self.inactive.pk)
        queue = self.client.get(reverse("opd_patient_queue"), {"patient": self.patient.pk})
        self.assertContains(queue, "inactive_doctor")
        self.assertEqual(queue.context["selected"]["status"], "Doctor Assigned")

    def test_doctor_assignment_does_not_override_active_consultation_status(self):
        self.patient.assigned_doctor = self.doctor
        self.patient.save(update_fields=["assigned_doctor"])
        ConsultationSession.objects.create(patient=self.patient, doctor=self.doctor,
                                           status=ConsultationSession.Status.IN_PROGRESS)
        row = _opd_patient_flow([self.patient])[0]
        self.assertEqual(row["status"], "With Doctor")
        self.assertFalse(row["needs_doctor"])
