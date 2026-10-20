import json
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.core.management import call_command

from core.models import UserProfile
from patients.models import Patient, PatientBill, OPDReferral, ConsultationSession, ClinicalOrder, OPDAuditEvent, InitialAssessment, PrescriptionItem

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

        prescription = consultation.prescriptions.get()
        prescription.dispense_status = PrescriptionItem.DispenseStatus.PARTIAL
        prescription.dispensed_quantity = 2
        prescription.save()
        order = consultation.orders.get()
        order.status = "Completed"
        order.save()
        payload["consultation_id"] = consultation.pk
        repeated_response = self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(repeated_response.status_code, 200)
        prescription.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(consultation.prescriptions.count(), 1)
        self.assertEqual(prescription.dispense_status, PrescriptionItem.DispenseStatus.PARTIAL)
        self.assertEqual(order.status, "Completed")


class OpdAssistantAccessTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.opd_user = User.objects.create_user(username="opd_staff", password="password123")
        self.opd_user.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        self.opd_user.profile.save()
        self.doctor_user = User.objects.create_user(username="another_doctor", password="password123")
        self.doctor_user.profile.designation = UserProfile.Designation.DOCTOR
        self.doctor_user.profile.save()

    def test_opd_assistant_can_open_opd_dashboard(self):
        self.client.login(username="opd_staff", password="password123")
        response = self.client.get(reverse("opd_assistant_dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_other_designation_is_redirected_from_opd_dashboard(self):
        self.client.login(username="another_doctor", password="password123")
        response = self.client.get(reverse("opd_assistant_dashboard"))
        self.assertRedirects(response, reverse("dashboard"))

    def test_patient_and_service_queue_workflow(self):
        self.client.login(username="opd_staff", password="password123")
        patient = Patient.objects.create(first_name="Mira", last_name="Das", mr_number="MR-1001")
        consultation = ConsultationSession.objects.create(
            patient=patient,
            visit_token="OPD-TEST-1001",
            status=ConsultationSession.Status.IN_PROGRESS,
        )
        order = ClinicalOrder.objects.create(
            consultation=consultation,
            order_type=ClinicalOrder.OrderType.LABORATORY,
            name="Complete Blood Count",
        )

        queue_response = self.client.get(reverse("opd_patient_queue"), {"patient": patient.pk})
        self.assertEqual(queue_response.status_code, 200)
        self.assertContains(queue_response, "Mira Das")

        service_response = self.client.get(reverse("opd_service_queue", args=["laboratory"]))
        self.assertEqual(service_response.status_code, 200)
        self.assertContains(service_response, "Complete Blood Count")

        update_response = self.client.post(reverse("opd_order_update", args=[order.pk]), {
            "status": "Review Required",
            "result_summary": "Verified report ready for clinician review",
        })
        self.assertRedirects(update_response, reverse("opd_service_queue", args=["laboratory"]))
        order.refresh_from_db()
        self.assertEqual(order.status, "Review Required")
        event = OPDAuditEvent.objects.get(order=order)
        self.assertEqual(event.actor, self.opd_user)

    def test_assessment_draft_and_complete_routing(self):
        self.client.login(username="opd_staff", password="password123")
        patient = Patient.objects.create(first_name="Arun", last_name="Rai")
        url = reverse("save_initial_assessment")

        draft_response = self.client.post(
            url,
            data=json.dumps({"patient_id": patient.pk, "action": "draft", "complaints": ""}),
            content_type="application/json",
        )
        self.assertEqual(draft_response.status_code, 200)
        assessment = InitialAssessment.objects.get(patient=patient)
        self.assertEqual(assessment.status, InitialAssessment.Status.DRAFT)

        missing_complaint = self.client.post(
            url,
            data=json.dumps({"patient_id": patient.pk, "action": "complete", "complaints": ""}),
            content_type="application/json",
        )
        self.assertEqual(missing_complaint.status_code, 400)

        invalid_vital = self.client.post(
            url,
            data=json.dumps({"patient_id": patient.pk, "action": "draft", "spo2": 140}),
            content_type="application/json",
        )
        self.assertEqual(invalid_vital.status_code, 400)

        complete_response = self.client.post(
            url,
            data=json.dumps({"patient_id": patient.pk, "action": "complete", "complaints": "Cough"}),
            content_type="application/json",
        )
        self.assertEqual(complete_response.status_code, 200)
        assessment.refresh_from_db()
        self.assertEqual(assessment.status, InitialAssessment.Status.COMPLETED)
        self.assertEqual(assessment.completed_by, self.opd_user)
        self.assertTrue(OPDAuditEvent.objects.filter(patient=patient, action="assessment_completed").exists())

    def test_registration_role_cannot_save_assessment(self):
        self.client.login(username="another_doctor", password="password123")
        self.doctor_user.profile.designation = UserProfile.Designation.REGISTRATION_DESK
        self.doctor_user.profile.save()
        patient = Patient.objects.create(first_name="Tara", last_name="Sen")
        response = self.client.post(
            reverse("save_initial_assessment"),
            data=json.dumps({"patient_id": patient.pk, "action": "draft"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_pharmacy_dispense_respects_pending_payment(self):
        self.client.login(username="opd_staff", password="password123")
        patient = Patient.objects.create(first_name="Nila", last_name="Roy")
        consultation = ConsultationSession.objects.create(patient=patient, visit_token="OPD-PHARM-1")
        item = PrescriptionItem.objects.create(
            consultation=consultation,
            medicine_name="Demo medication",
            dose="1 tablet",
            route="Oral",
            frequency="Once daily",
            duration="3 days",
        )
        bill = PatientBill.objects.create(
            patient=patient,
            source=PatientBill.Source.DOCTOR,
            description="Demo prescription",
            amount="100.00",
        )
        queue_response = self.client.get(
            reverse("opd_service_queue", args=["pharmacy"]), {"prescription": item.pk}
        )
        self.assertEqual(queue_response.status_code, 200)
        self.assertContains(queue_response, "Awaiting payment")

        dispense_url = reverse("opd_prescription_update", args=[item.pk])
        payload = {"dispense_status": "dispensed", "stock_status": "available", "dispensed_quantity": "3"}
        self.client.post(dispense_url, payload)
        item.refresh_from_db()
        self.assertEqual(item.dispense_status, PrescriptionItem.DispenseStatus.ORDERED)

        bill.status = PatientBill.Status.PAID
        bill.save(update_fields=["status"])
        self.client.post(dispense_url, payload)
        item.refresh_from_db()
        self.assertEqual(item.dispense_status, PrescriptionItem.DispenseStatus.DISPENSED)
        self.assertEqual(item.dispensed_by, self.opd_user)

    def test_service_specific_fields_and_counselling_note_protection(self):
        self.client.login(username="opd_staff", password="password123")
        patient = Patient.objects.create(first_name="Lena", last_name="Fern")
        consultation = ConsultationSession.objects.create(patient=patient, visit_token="OPD-SERVICE-1")
        procedure = ClinicalOrder.objects.create(
            consultation=consultation,
            order_type=ClinicalOrder.OrderType.OTHER,
            name="Procedure Room demo procedure",
        )
        procedure_page = self.client.get(
            reverse("opd_service_queue", args=["procedure-room"]), {"order": procedure.pk}
        )
        self.assertContains(procedure_page, "Consent confirmed")
        self.client.post(reverse("opd_order_update", args=[procedure.pk]), {
            "service": "procedure-room",
            "status": "In Progress",
            "room": "Room 2",
            "assigned_staff": "Demo staff",
            "consent_confirmed": "on",
            "preparation_complete": "on",
            "post_instructions": "Synthetic demo instruction",
        })
        procedure.refresh_from_db()
        self.assertEqual(procedure.operational_details["room"], "Room 2")
        self.assertTrue(procedure.operational_details["consent_confirmed"])

        counselling = ClinicalOrder.objects.create(
            consultation=consultation,
            order_type=ClinicalOrder.OrderType.OTHER,
            name="Counselling session",
            result_summary="Sensitive clinician-only note",
        )
        counselling_page = self.client.get(
            reverse("opd_service_queue", args=["counselling"]), {"order": counselling.pk}
        )
        self.assertNotContains(counselling_page, "Sensitive clinician-only note")

    def test_demo_seed_is_idempotent_and_labels_patients(self):
        call_command("seed_opd_demo", verbosity=0)
        call_command("seed_opd_demo", verbosity=0)
        demo_patients = Patient.objects.filter(is_demo=True)
        self.assertEqual(demo_patients.count(), 9)
        self.assertEqual(
            ConsultationSession.objects.filter(patient__is_demo=True).count(), 6
        )

    def test_referral_submission_enters_destination_queue_and_audit(self):
        self.client.login(username="opd_staff", password="password123")
        patient = Patient.objects.create(first_name="Sana", last_name="Paul", mr_number="MR-REF-1")
        response = self.client.post(reverse("opd_referrals"), {
            "patient_id": patient.pk,
            "referral_type": OPDReferral.ReferralType.INTERNAL,
            "department": "Cardiology",
            "reason": "Cross-consultation requested",
            "urgency": OPDReferral.Urgency.URGENT,
            "action": "send",
        })
        referral = OPDReferral.objects.get(patient=patient)
        self.assertRedirects(response, f"{reverse('opd_referrals')}?patient={patient.pk}")
        self.assertEqual(referral.status, OPDReferral.Status.SENT)
        self.assertEqual(referral.created_by, self.opd_user)
        self.assertTrue(OPDAuditEvent.objects.filter(patient=patient, action="referral_sent").exists())

    def test_supporting_pages_and_csv_export(self):
        self.client.login(username="opd_staff", password="password123")
        for module in ("reports", "master-data", "audit-trail", "settings"):
            response = self.client.get(reverse("opd_module", args=[module]))
            self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("opd_module", args=["settings"]), {
            "notifications": "on",
            "compact_tables": "on",
        })
        self.assertRedirects(response, reverse("opd_module", args=["settings"]))
        self.assertTrue(self.client.session["opd_preferences"]["compact_tables"])
        export_response = self.client.get(reverse("opd_module", args=["reports"]), {"export": "csv"})
        self.assertEqual(export_response["Content-Type"], "text/csv")

    def test_dashboard_search_and_filtered_export(self):
        self.client.login(username="opd_staff", password="password123")
        Patient.objects.create(first_name="Demo Search", last_name="Patient", mr_number="MR-FLOW-77")
        Patient.objects.create(first_name="Other", last_name="Patient", mr_number="MR-FLOW-88")
        dashboard = reverse("opd_assistant_dashboard")
        response = self.client.get(dashboard, {"q": "MR-FLOW-77"})
        self.assertContains(response, "Demo Search Patient")
        self.assertNotContains(response, "MR-FLOW-88")
        export = self.client.get(dashboard, {"q": "MR-FLOW-77", "export": "csv"})
        self.assertEqual(export["Content-Type"], "text/csv")
        self.assertContains(export, "MR-FLOW-77")
        self.assertNotContains(export, "MR-FLOW-88")

    def test_patient_bill_slip_rendering_with_none_created_by(self):
        reg_user = get_user_model().objects.create_user(username="reg_print_user", password="password123")
        reg_user.profile.designation = UserProfile.Designation.REGISTRATION_DESK
        reg_user.profile.save()
        self.client.login(username="reg_print_user", password="password123")
        patient = Patient.objects.create(first_name="Rohan", last_name="Khan")
        bill = PatientBill.objects.create(
            patient=patient,
            amount=Decimal("150.00"),
            status=PatientBill.Status.PENDING,
            created_by=None,
            description="Consultation fee",
        )
        response = self.client.get(reverse("patient_bill_slip", args=[bill.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Payment Slip")
        self.assertContains(response, "Registration Desk")

    def test_reception_dashboard_mindbloom_rendering(self):
        reg_user = get_user_model().objects.create_user(username="reception_agent", password="password123")
        reg_user.profile.designation = UserProfile.Designation.REGISTRATION_DESK
        reg_user.profile.save()
        self.client.login(username="reception_agent", password="password123")
        response = self.client.get(reverse("reception_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mb-bento")
        self.assertContains(response, "Patient Intake &amp; Flow Overview")
        self.assertContains(response, "Counter Flow Activity")
        self.assertContains(response, "Queue Triage Roster")

    def test_opd_assistant_dashboard_mindbloom_rendering(self):
        opd_user = get_user_model().objects.create_user(username="opd_mindbloom_user", password="password123")
        opd_user.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        opd_user.profile.save()
        self.client.login(username="opd_mindbloom_user", password="password123")
        response = self.client.get(reverse("opd_assistant_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mb-bento")
        self.assertContains(response, "Outpatient Flow &amp; Capacity Overview")
        self.assertContains(response, "OPD Patient Flow Activity")
        self.assertContains(response, "Priority Patient Roster")
        self.assertContains(response, "opd-triage-card")
        self.assertContains(response, "id=\"openWidgetsBtn\"")
        self.assertContains(response, "id=\"widgetsModal\"")
        self.assertContains(response, "mb-modal-window")


