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

    def test_doctor_consultation_ready_queue(self):
        """Patients with a completed initial assessment appear in the ready queue."""
        InitialAssessment.objects.create(
            patient=self.patient,
            status=InitialAssessment.Status.COMPLETED,
            summary_note="Triage complete",
        )
        response = self.client.get(reverse("doctor_consultation"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready for consultation")
        self.assertContains(response, "Anita Sharma")

    def test_doctor_consultation_patient_workspace(self):
        """Test consultation workstation renders the six-step doctor workflow."""
        url = reverse("doctor_consultation") + f"?patient={self.patient.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Anita Sharma")
        self.assertContains(response, "Assessment")
        self.assertContains(response, "Prescription")
        self.assertContains(response, "Hospital Formulary")

    def test_workspace_renders_unassigned_nurse_task(self):
        """An open task without an assignee must not crash the open-tasks table."""
        from patients.models import NurseTask

        NurseTask.objects.create(
            patient=self.patient, title="Collect sample", status="pending"
        )
        url = reverse("doctor_consultation") + f"?patient={self.patient.pk}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Collect sample")
        self.assertContains(response, "Unassigned")

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

    def test_opd_assistant_can_switch_workspaces(self):
        opd_url = reverse("opd_assistant_dashboard")
        inpatient_url = reverse("opd_inpatient_dashboard")
        login = self.client.post(reverse("login"), {
            "username": "opd_staff",
            "password": "password123",
            "designation": UserProfile.Designation.OPD_ASSISTANT,
        })
        self.assertRedirects(login, opd_url)

        response = self.client.get(opd_url)
        self.assertContains(response, f'<a href="{opd_url}" aria-current="true">OPD</a>', html=True)
        self.assertContains(response, f'<a href="{inpatient_url}">Inpatient</a>', html=True)
        self.assertContains(response, "Assessment Queue")
        self.assertRegex(response.content.decode(), r'</nav>\s*<div class="side-status-dock">')
        self.assertContains(response, 'id="opdToolsPanel"')

        response = self.client.get(inpatient_url)
        self.assertContains(response, f'<a href="{opd_url}">OPD</a>', html=True)
        self.assertContains(response, f'<a href="{inpatient_url}" aria-current="true">Inpatient</a>', html=True)
        self.assertContains(response, "<h1>Inpatient Desk</h1>", html=True)
        self.assertContains(response, 'id="receptionNavToggle"')
        self.assertNotContains(response, "Assessment Queue")
        self.assertNotContains(response, "Search patient register")

    def test_inpatient_requires_opd_assistant_access(self):
        url = reverse("opd_inpatient_dashboard")
        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")
        self.client.force_login(self.doctor_user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_patient_and_service_queue_workflow(self):
        self.client.login(username="opd_staff", password="password123")
        patient = Patient.objects.create(first_name="Mira", last_name="Das", uhid="MR-1001")
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

    def test_opd_assistant_cannot_access_pharmacy_queue_or_dispensing(self):
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
        queue_response = self.client.get(reverse("opd_service_queue", args=["pharmacy"]))
        self.assertEqual(queue_response.status_code, 403)

        response = self.client.post(reverse("opd_prescription_update", args=[item.pk]), {
            "dispense_status": "dispensed",
            "stock_status": "available",
            "dispensed_quantity": "3",
        })
        self.assertEqual(response.status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.dispense_status, PrescriptionItem.DispenseStatus.ORDERED)

    def test_service_specific_fields_and_counselling_note_protection(self):
        self.client.login(username="opd_staff", password="password123")
        patient = Patient.objects.create(first_name="Lena", last_name="Fern")
        consultation = ConsultationSession.objects.create(patient=patient, visit_token="OPD-SERVICE-1")
        from patients.models import ServiceDepartment
        procedure = ClinicalOrder.objects.create(
            department=ServiceDepartment.objects.get(slug="procedure-room"),
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

            "consent_confirmed": "on",
            "preparation_complete": "on",
            "post_instructions": "Synthetic demo instruction",
        })
        procedure.refresh_from_db()
        self.assertEqual(procedure.operational_details["room"], "Room 2")
        self.assertTrue(procedure.operational_details["consent_confirmed"])

        counselling = ClinicalOrder.objects.create(
            department=ServiceDepartment.objects.get(slug="counselling"),
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
        patient = Patient.objects.create(first_name="Sana", last_name="Paul", uhid="MR-REF-1")
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
        Patient.objects.create(first_name="Demo Search", last_name="Patient", uhid="MR-FLOW-77")
        Patient.objects.create(first_name="Other", last_name="Patient", uhid="MR-FLOW-88")
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


    def test_opd_assistant_dashboard_mindbloom_rendering(self):
        opd_user = get_user_model().objects.create_user(username="opd_mindbloom_user", password="password123")
        opd_user.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        opd_user.profile.save()
        self.client.login(username="opd_mindbloom_user", password="password123")
        response = self.client.get(reverse("opd_assistant_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mb-bento")
        self.assertContains(response, "Outpatient Flow &amp; Capacity Overview")
        self.assertContains(response, "On-Duty Doctors")

        self.assertNotContains(response, "opd-triage-card")
        self.assertContains(response, "Patient Queue")




class OPDTriageTests(TestCase):
    """OPD assistant triage: the OPD Desk records initial assessments directly.

    The standalone Nurse designation was merged into OPD Assistant — the triage
    workstation, assessment queue, nursing tasks and handover sheets are all
    part of the OPD Desk now.
    """

    def setUp(self):
        self.client = Client()
        self.opd = User.objects.create_user(
            username="opd_desk", password="password123", first_name="Desk", last_name="One"
        )
        opd_profile, _ = UserProfile.objects.get_or_create(user=self.opd)
        opd_profile.designation = UserProfile.Designation.OPD_ASSISTANT
        opd_profile.save()

        # Legacy account whose profile still stores the removed designation.
        self.legacy_nurse = User.objects.create_user(
            username="nurse_meera", password="password123", first_name="Meera", last_name="Nair"
        )
        legacy_profile, _ = UserProfile.objects.get_or_create(user=self.legacy_nurse)
        legacy_profile.designation = "nurse"
        legacy_profile.save()

        self.patient = Patient.objects.create(
            first_name="Ravi", last_name="Kumar", uhid="UH-1001",
            gender=Patient.Gender.MALE, phone="9876543210",
        )

    def _login_opd(self):
        self.client.login(username="opd_desk", password="password123")
        session = self.client.session
        session["designation"] = "opd_assistant"
        session.save()

    def _post_json(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json")

    def test_login_redirects_opd_assistant_to_opd_dashboard(self):
        response = self.client.post(reverse("login"), {
            "username": "opd_desk", "password": "password123", "designation": "opd_assistant",
        })
        self.assertRedirects(response, reverse("opd_assistant_dashboard"))
        self.assertEqual(self.client.session["designation"], "opd_assistant")

    def test_opd_assistant_cannot_log_in_as_doctor(self):
        response = self.client.post(reverse("login"), {
            "username": "opd_desk", "password": "password123", "designation": "doctor",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Access denied")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_legacy_nurse_designation_is_treated_as_opd_assistant(self):
        # The login dropdown no longer offers the Nurse option…
        login_page = self.client.get(reverse("login"))
        self.assertContains(login_page, "opd_assistant")
        self.assertNotContains(login_page, 'value="nurse"')

        # …and a legacy nurse profile can no longer log in as "nurse".
        response = self.client.post(reverse("login"), {
            "username": "nurse_meera", "password": "password123", "designation": "nurse",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Access denied")
        self.assertNotIn("_auth_user_id", self.client.session)

        # It logs in as OPD Assistant instead and keeps working there.
        response = self.client.post(reverse("login"), {
            "username": "nurse_meera", "password": "password123", "designation": "opd_assistant",
        })
        self.assertRedirects(response, reverse("opd_assistant_dashboard"))
        self.assertEqual(self.client.session["designation"], "opd_assistant")

        # Old bookmarks (/nurse/) forward to the unified OPD dashboard.
        self.assertRedirects(self.client.get(reverse("nurse_portal")), reverse("opd_assistant_dashboard"))

    def test_opd_assistant_is_kept_out_of_doctor_and_reception_portals(self):
        self._login_opd()
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("opd_assistant_dashboard"))
        self.assertRedirects(self.client.get(reverse("reception_dashboard")), reverse("opd_assistant_dashboard"))
        self.assertRedirects(self.client.get(reverse("doctor_consultation")), reverse("opd_assistant_dashboard"))

    def test_nursing_dashboard_counts_match_queue_and_tasks(self):
        from patients.models import NurseQueueTicket, NurseTask
        from django.utils import timezone
        self._login_opd()
        urgent = NurseQueueTicket.objects.create(patient=self.patient, priority="urgent", status="recalled")
        other = Patient.objects.create(first_name="Another", last_name="Patient")
        routine = NurseQueueTicket.objects.create(patient=other, priority="routine", status="in_assessment", claimed_by=self.opd)
        NurseTask.objects.create(patient=self.patient, title="Repeat vitals", priority="urgent", status="pending", due_at=timezone.now())
        response = self.client.get(reverse("opd_assistant_dashboard"), {"q": "Ravi"})
        self.assertEqual(response.context["nursing_counts"], {"awaiting": 1, "assessing": 1, "urgent": 1, "tasks": 1})
        self.assertEqual(list(response.context["assessment_preview"]), [urgent, routine])
        self.assertEqual(len(response.context["task_preview"]), 1)
        self.assertEqual(response.context["task_preview"][0].title, "Repeat vitals")
        self.assertEqual(list(self.client.get(reverse("nurse_queue"), {"status": "awaiting"}).context["tickets"]), [urgent])
        self.assertEqual(list(self.client.get(reverse("nurse_queue"), {"status": "open", "priority": "urgent_all"}).context["tickets"]), [urgent])
        self.assertEqual(self.client.get(reverse("nurse_tasks"), {"status": "open"}).context["counts"]["open"], 1)

    def test_open_task_count_can_be_browsed_across_pages(self):
        from patients.models import NurseTask
        self._login_opd()
        NurseTask.objects.bulk_create([
            NurseTask(patient=self.patient, title=f"Task {number}", status="pending")
            for number in range(51)
        ])
        response = self.client.get(reverse("nurse_tasks"), {"status": "open"})
        self.assertEqual(response.context["counts"]["open"], 51)
        self.assertEqual(len(response.context["tasks"]), 50)
        self.assertTrue(response.context["tasks"].has_next())
        second = self.client.get(reverse("nurse_tasks"), {"status": "open", "page": 2})
        self.assertEqual(len(second.context["tasks"]), 1)

    def test_second_claim_cannot_replace_existing_owner(self):
        from patients.models import NurseQueueTicket
        self._login_opd()
        ticket = NurseQueueTicket.objects.create(patient=self.patient, status="waiting")
        self.client.post(reverse("nurse_claim_ticket", args=[ticket.pk]))
        ticket.refresh_from_db()
        self.assertEqual(ticket.claimed_by, self.opd)
        self.client.force_login(self.legacy_nurse)
        response = self.client.post(reverse("nurse_claim_ticket", args=[ticket.pk]))
        self.assertRedirects(response, reverse("nurse_queue"))
        ticket.refresh_from_db()
        self.assertEqual(ticket.claimed_by, self.opd)
        response = self.client.post(reverse("opd_send_to_nurse"), {"patient_id": self.patient.pk})
        self.assertRedirects(response, reverse("nurse_queue"))
        ticket.refresh_from_db()
        self.assertEqual(ticket.claimed_by, self.opd)
        self.assertEqual(ticket.status, NurseQueueTicket.Status.IN_ASSESSMENT)

    def test_opd_assistant_opens_the_triage_tabs(self):
        self._login_opd()
        queue = self.client.get(reverse("nurse_queue"))
        self.assertEqual(queue.status_code, 200)
        self.assertContains(queue, "Assessment Queue")
        triage = self.client.get(reverse("nurse_triage"))
        self.assertEqual(triage.status_code, 200)
        tasks = self.client.get(reverse("nurse_tasks"))
        self.assertEqual(tasks.status_code, 200)
        handover = self.client.get(reverse("nurse_handover"))
        self.assertEqual(handover.status_code, 200)
        self.assertContains(handover, "Shift Handover Sheet")

    def test_registration_desk_cannot_open_the_triage_workstation(self):
        reg = User.objects.create_user(username="reg_desk", password="password123")
        profile, _ = UserProfile.objects.get_or_create(user=reg)
        profile.designation = UserProfile.Designation.REGISTRATION_DESK
        profile.save()
        self.client.login(username="reg_desk", password="password123")
        session = self.client.session
        session["designation"] = "registration_desk"
        session.save()
        self.assertRedirects(self.client.get(reverse("nurse_queue")), reverse("reception_dashboard"))
        self.assertRedirects(self.client.get(reverse("nurse_triage")), reverse("reception_dashboard"))

    def test_opd_starts_and_completes_assessment_in_one_flow(self):
        # 1. The OPD Desk starts the assessment straight from the patient row —
        #    the ticket is created and claimed in a single step.
        self._login_opd()
        from patients.models import NurseQueueTicket
        response = self.client.post(reverse("opd_send_to_nurse"), {
            "patient_id": self.patient.pk, "priority": "urgent", "note": "BP check requested",
        })
        ticket = NurseQueueTicket.objects.get(patient=self.patient)
        self.assertRedirects(response, reverse("nurse_triage") + f"?ticket={ticket.pk}")
        self.assertEqual(ticket.status, NurseQueueTicket.Status.IN_ASSESSMENT)
        self.assertEqual(ticket.priority, NurseQueueTicket.Priority.URGENT)
        self.assertEqual(ticket.sent_by_id, self.opd.pk)
        self.assertEqual(ticket.claimed_by_id, self.opd.pk)

        # In the OPD flow the visit now reads as being in assessment.
        flow = self.client.get(reverse("opd_patient_queue") + f"?patient={self.patient.pk}")
        self.assertContains(flow, "triage in progress")

        # 2. The OPD assistant records vitals and sends the visit back to the desk.
        response = self._post_json(reverse("nurse_save_triage"), {
            "ticket_id": ticket.pk,
            "patient_id": self.patient.pk,
            "action": "complete",
            "temperature": "38.6",
            "pulse": "122",
            "bp_systolic": "138",
            "bp_diastolic": "86",
            "spo2": "94",
            "respiratory_rate": "22",
            "complaints": [{"text": "Fever with chills", "duration": "2 days"}],
            "hpi": "High grade fever since two days",
            "eta_minutes": "10",
            "examination": {"general": "Febrile"},
            "red_flags": {},
            "orders": {"laboratory": ["CBC"], "radiology": [], "other": []},
        })
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["routed_to"], "opd_desk")
        self.assertEqual(data["band"]["label"], "Semi-urgent")
        self.assertGreaterEqual(data["priority_score"], 40)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, NurseQueueTicket.Status.COMPLETED)
        self.assertEqual(ticket.completed_by_id, self.opd.pk)
        self.assertIsNotNone(ticket.returned_at)
        self.assertEqual(ticket.eta_minutes, 10)
        self.assertEqual(ticket.priority, NurseQueueTicket.Priority.URGENT)

        assessment = self.patient.initial_assessment
        self.assertEqual(assessment.status, InitialAssessment.Status.COMPLETED)
        self.assertEqual(assessment.bp_systolic, 138)
        self.assertEqual(assessment.orders["laboratory"], ["CBC"])
        self.assertEqual(assessment.completed_by_id, self.opd.pk)

        # 3. The visit is back at the desk, ready for doctor allocation.
        flow = self.client.get(reverse("opd_patient_queue") + f"?patient={self.patient.pk}")
        self.assertContains(flow, "Initial assessment complete")

    def test_complete_requires_vitals_and_complaint(self):
        self._login_opd()
        response = self._post_json(reverse("nurse_save_triage"), {
            "patient_id": self.patient.pk, "action": "complete",
            "complaints": [{"text": "Headache"}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("core vital", response.json()["error"])

        response = self._post_json(reverse("nurse_save_triage"), {
            "patient_id": self.patient.pk, "action": "complete", "pulse": "80",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("chief complaint", response.json()["error"])

    def test_draft_keeps_visit_in_assessment_queue(self):
        self._login_opd()
        response = self._post_json(reverse("nurse_save_triage"), {
            "patient_id": self.patient.pk, "action": "draft", "pulse": "88", "spo2": "98",
        })
        self.assertTrue(response.json()["ok"])
        from patients.models import NurseQueueTicket
        ticket = NurseQueueTicket.objects.get(patient=self.patient)
        self.assertEqual(ticket.status, NurseQueueTicket.Status.IN_ASSESSMENT)
        self.assertEqual(ticket.claimed_by_id, self.opd.pk)

    def test_red_flag_escalates_and_marks_emergency(self):
        self._login_opd()
        response = self._post_json(reverse("nurse_save_triage"), {
            "patient_id": self.patient.pk, "action": "complete",
            "pulse": "118", "spo2": "88", "pain_score": "9",
            "complaints": [{"text": "Chest pain", "duration": "Few hours"}],
            "red_flags": {"chest_pain": True, "breathlessness": True},
        })
        data = response.json()
        self.assertEqual(data["band"]["label"], "Emergency")
        self.assertTrue(data["escalated"])
        self.patient.refresh_from_db()
        self.assertTrue(self.patient.is_emergency)

    def test_walk_in_triage_opens_ticket(self):
        self._login_opd()
        response = self.client.get(reverse("nurse_triage") + f"?patient={self.patient.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Triage Workstation")
        self.assertContains(response, "Ravi Kumar")
        from patients.models import NurseQueueTicket
        ticket = NurseQueueTicket.objects.get(patient=self.patient)
        self.assertEqual(ticket.status, NurseQueueTicket.Status.IN_ASSESSMENT)
        self.assertEqual(ticket.source, NurseQueueTicket.Source.REGISTRATION)

    def test_nursing_task_board_create_and_complete(self):
        self._login_opd()
        self.client.post(reverse("nurse_tasks"), {
            "patient_id": self.patient.pk, "title": "Wound dressing",
            "kind": "dressing", "priority": "urgent", "assign_me": "on",
        })
        from patients.models import NurseTask
        task = NurseTask.objects.get(patient=self.patient)
        self.assertEqual(task.assigned_to_id, self.opd.pk)

        self.client.post(reverse("nurse_task_update", args=[task.pk]), {
            "status": "done", "outcome": "Dressing changed",
        })
        task.refresh_from_db()
        self.assertEqual(task.status, NurseTask.Status.DONE)
        self.assertEqual(task.outcome, "Dressing changed")

    def test_handover_and_patient_search(self):
        self._login_opd()
        handover = self.client.get(reverse("nurse_handover"))
        self.assertEqual(handover.status_code, 200)
        self.assertContains(handover, "Shift Handover Sheet")

        search = self.client.get(reverse("nurse_patient_search") + "?q=Ravi")
        self.assertEqual(search.status_code, 200)
        self.assertTrue(any(r["name"] == "Ravi Kumar" for r in search.json()["results"]))

    def test_doctor_self_picks_unassigned_assessed_patient(self):
        from django.utils import timezone as dj_tz
        # The OPD assistant records the assessment; no doctor is assigned yet.
        self._login_opd()
        self._post_json(reverse("nurse_save_triage"), {
            "patient_id": self.patient.pk, "action": "complete",
            "pulse": "88", "spo2": "98", "temperature": "37.2",
            "complaints": [{"text": "Fever", "duration": "1 day"}],
        })
        self.patient.refresh_from_db()
        self.assertIsNone(self.patient.assigned_doctor)

        # The doctor's consultation queue lists the visit as unassigned.
        doctor = User.objects.create_user(
            username="dr_selfpick", password="password123", first_name="Self", last_name="Pick"
        )
        profile, _ = UserProfile.objects.get_or_create(user=doctor)
        profile.designation = UserProfile.Designation.DOCTOR
        profile.save()
        self.client.login(username="dr_selfpick", password="password123")
        session = self.client.session
        session["designation"] = "doctor"
        session.save()
        queue = self.client.get(reverse("doctor_consultation"))
        self.assertEqual(queue.status_code, 200)
        self.assertContains(queue, "Unassigned — waiting for a doctor")
        self.assertContains(queue, "Ravi Kumar")

        # The doctor picks the patient themselves — assignment lands on him.
        response = self.client.post(
            reverse("assign_doctor", args=[self.patient.pk]),
            {"doctor_id": doctor.pk, "next": reverse("doctor_consultation")},
        )
        self.assertRedirects(response, reverse("doctor_consultation"))
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor_id, doctor.pk)
        self.assertTrue(OPDAuditEvent.objects.filter(
            patient=self.patient, actor=doctor, action="doctor_assigned",
        ).exists())

        # The picked patient now appears in the doctor's ready rail.
        queue = self.client.get(reverse("doctor_consultation"))
        self.assertContains(queue, "assigned to you")

    def test_completed_assessment_without_ticket_flows_to_doctor_allocation(self):
        # Assessments recorded from the shared workstation (no triage ticket)
        # must still move the visit to Doctor Allocation, matching the
        # assign_doctor gate.
        from django.utils import timezone as dj_tz
        from core.views import _opd_patient_flow

        InitialAssessment.objects.create(
            patient=self.patient,
            status=InitialAssessment.Status.COMPLETED,
            completed_at=dj_tz.now(),
            complaints="Fever",
        )
        rows = _opd_patient_flow([self.patient])
        self.assertEqual(rows[0]["status"], "Doctor Allocation")
        self.assertTrue(rows[0]["needs_doctor"])


class DoctorPatientQueueTests(TestCase):
    """The doctor's central queue: my patients, the unassigned pool, picking."""

    def setUp(self):
        from django.utils import timezone

        self.client = Client()
        self.doctor = User.objects.create_user(
            username="dr_queue", password="password123", first_name="Q", last_name="Doc"
        )
        profile, _ = UserProfile.objects.get_or_create(user=self.doctor)
        profile.designation = UserProfile.Designation.DOCTOR
        profile.save()

        self.opd = User.objects.create_user(username="queue_opd", password="password123")
        opd_profile, _ = UserProfile.objects.get_or_create(user=self.opd)
        opd_profile.designation = UserProfile.Designation.OPD_ASSISTANT
        opd_profile.save()

        self.mine = Patient.objects.create(
            first_name="Ada", last_name="Mine", uhid="Q-1", gender=Patient.Gender.FEMALE
        )
        self.mine.assigned_doctor = self.doctor
        self.mine.save()
        InitialAssessment.objects.create(
            patient=self.mine,
            status=InitialAssessment.Status.COMPLETED,
            completed_at=timezone.now(),
            complaints="Fever",
        )

        self.pool_patient = Patient.objects.create(
            first_name="Bo", last_name="Pool", uhid="Q-2", gender=Patient.Gender.MALE
        )
        InitialAssessment.objects.create(
            patient=self.pool_patient,
            status=InitialAssessment.Status.COMPLETED,
            completed_at=timezone.now(),
            complaints="Cough",
        )

    def _login_doctor(self):
        self.client.login(username="dr_queue", password="password123")
        session = self.client.session
        session["designation"] = "doctor"
        session.save()

    def test_doctor_queue_lists_mine_and_unassigned_pool(self):
        self._login_doctor()
        page = self.client.get(reverse("patient_queue"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "My patients")
        self.assertContains(page, "Ada Mine")
        self.assertContains(page, "Waiting to start")
        self.assertContains(page, "Unassigned — waiting for a doctor")
        self.assertContains(page, "Bo Pool")
        self.assertContains(page, "Pick this patient")

    def test_doctor_picks_from_the_queue(self):
        self._login_doctor()
        response = self.client.post(
            reverse("assign_doctor", args=[self.pool_patient.pk]),
            {"doctor_id": self.doctor.pk, "next": reverse("patient_queue")},
        )
        self.assertRedirects(response, reverse("patient_queue"))
        self.pool_patient.refresh_from_db()
        self.assertEqual(self.pool_patient.assigned_doctor_id, self.doctor.pk)

        # The picked patient has moved out of the pool and into My patients.
        page = self.client.get(reverse("patient_queue"))
        self.assertContains(page, "Bo Pool")
        self.assertNotContains(page, "Pick this patient")

    def test_all_toggle_lists_older_assigned_patients(self):
        from datetime import timedelta

        from django.utils import timezone

        older = Patient.objects.create(first_name="Cy", last_name="Older", uhid="Q-3")
        older.assigned_doctor = self.doctor
        older.save(update_fields=["assigned_doctor"])
        Patient.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(days=5)
        )

        self._login_doctor()
        today_view = self.client.get(reverse("patient_queue"))
        self.assertContains(today_view, "Ada Mine")
        self.assertNotContains(today_view, "Cy Older")

        all_view = self.client.get(reverse("patient_queue") + "?all=1")
        self.assertContains(all_view, "Cy Older")
        self.assertContains(all_view, "Ada Mine")

    def test_opd_assistant_bounced_off_doctor_queue(self):
        self.client.login(username="queue_opd", password="password123")
        session = self.client.session
        session["designation"] = "opd_assistant"
        session.save()
        self.assertRedirects(
            self.client.get(reverse("patient_queue")),
            reverse("opd_assistant_dashboard"),
        )

    def test_service_hub_lists_departments(self):
        from patients.models import ServiceDepartment

        self._login_doctor()
        page = self.client.get(reverse("service_queues"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Service Queues")

        ServiceDepartment.objects.create(slug="queue-test-lab", name="Queue Test Laboratory")
        page = self.client.get(reverse("service_queues"))
        self.assertContains(page, "Queue Test Laboratory")


class ServiceDesignationAccessTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.service_user = User.objects.create_user(username="service_staff", password="password123")
        self.service_user.profile.designation = UserProfile.Designation.SERVICE_STAFF
        self.service_user.profile.save()
        from patients.models import ServiceDepartment
        self.service_user.profile.service_departments.add(ServiceDepartment.objects.get(slug="laboratory"))
        self.pharmacy_user = User.objects.create_user(username="pharmacy_staff", password="password123")
        self.pharmacy_user.profile.designation = UserProfile.Designation.PHARMACY
        self.pharmacy_user.profile.save()

    def test_login_routes_each_designation_to_its_dashboard(self):
        service_response = self.client.post(reverse("login"), {
            "username": "service_staff",
            "password": "password123",
            "designation": UserProfile.Designation.SERVICE_STAFF,
        })
        self.assertRedirects(service_response, reverse("service_staff_dashboard"))
        self.assertEqual(self.client.session["designation"], UserProfile.Designation.SERVICE_STAFF)

        self.client.get(reverse("logout"))
        pharmacy_response = self.client.post(reverse("login"), {
            "username": "pharmacy_staff",
            "password": "password123",
            "designation": UserProfile.Designation.PHARMACY,
        })
        self.assertRedirects(pharmacy_response, reverse("pharmacy_dashboard"))
        self.assertEqual(self.client.session["designation"], UserProfile.Designation.PHARMACY)

    def test_service_staff_can_process_non_pharmacy_orders_only(self):
        self.client.force_login(self.service_user)
        patient = Patient.objects.create(first_name="Asha", last_name="Sen", uhid="SVC-1001")
        consultation = ConsultationSession.objects.create(patient=patient, visit_token="SVC-1001")
        lab_order = ClinicalOrder.objects.create(
            consultation=consultation,
            order_type=ClinicalOrder.OrderType.LABORATORY,
            name="Complete Blood Count",
        )
        pharmacy_order = ClinicalOrder.objects.create(
            consultation=consultation,
            order_type=ClinicalOrder.OrderType.OTHER,
            name="Pharmacy service request",
        )

        dashboard = self.client.get(reverse("service_staff_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Laboratory")
        self.assertNotContains(dashboard, "Prescription worklist")
        self.assertEqual(self.client.get(reverse("opd_service_queue", args=["laboratory"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("opd_service_queue", args=["pharmacy"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("opd_patient_queue")).status_code, 302)

        update_response = self.client.post(reverse("opd_order_update", args=[lab_order.pk]), {
            "status": "Review Required",
            "result_summary": "Verified result summary",
        })
        self.assertEqual(update_response.status_code, 302)
        lab_order.refresh_from_db()
        self.assertEqual(lab_order.status, "Review Required")
        self.assertEqual(lab_order.result_summary, "Verified result summary")
        self.assertTrue(OPDAuditEvent.objects.filter(order=lab_order, actor=self.service_user).exists())

        denied_update = self.client.post(reverse("opd_order_update", args=[pharmacy_order.pk]), {
            "status": "Completed",
        })
        self.assertEqual(denied_update.status_code, 404)

    def test_pharmacy_staff_can_dispense_but_cannot_open_other_service_or_opd_queues(self):
        self.client.force_login(self.pharmacy_user)
        patient = Patient.objects.create(first_name="Nila", last_name="Roy", uhid="PHARM-1001")
        consultation = ConsultationSession.objects.create(patient=patient, visit_token="PHARM-1001")
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

        dashboard = self.client.get(reverse("pharmacy_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Prescription worklist")
        self.assertEqual(self.client.get(reverse("opd_service_queue", args=["pharmacy"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("opd_service_queue", args=["laboratory"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("opd_patient_queue")).status_code, 302)

        from datetime import timedelta
        from django.utils import timezone
        from patients.models import Medicine
        from core.operations import receive_stock, reserve_issue, handover_issue, set_bill_status
        from django.core.exceptions import ValidationError
        medicine = Medicine.objects.create(name=item.medicine_name)
        item.medicine = medicine
        item.prescribed_quantity = 3
        item.released_at = timezone.now()
        item.save()
        from decimal import Decimal
        from uuid import uuid4
        receive_stock(user=self.pharmacy_user, medicine=medicine, batch_number="ACCESS-BATCH", expiry_date=timezone.localdate() + timedelta(days=90), supplier="Test", invoice_reference="TEST", purchase_cost=Decimal("1"), selling_price=Decimal("2"), packs=3, request_key=uuid4())
        issue = reserve_issue(user=self.pharmacy_user, consultation=consultation, entries=[{"prescription_id": item.pk, "medicine_id": medicine.pk, "quantity": 3}], request_key=uuid4())
        with self.assertRaises(ValidationError):
            handover_issue(user=self.pharmacy_user, issue=issue)
        cashier = User.objects.create_user(username="access_cashier")
        cashier.profile.designation = UserProfile.Designation.REGISTRATION_DESK
        cashier.profile.save()
        set_bill_status(user=cashier, bill=issue.bill, next_status="paid")
        handover_issue(user=self.pharmacy_user, issue=issue)
        item.refresh_from_db()
        self.assertEqual(item.dispense_status, PrescriptionItem.DispenseStatus.DISPENSED)
        self.assertEqual(item.dispensed_by, self.pharmacy_user)
        self.assertTrue(OPDAuditEvent.objects.filter(actor=self.pharmacy_user, action="pharmacy_dispense_updated").exists())

    def test_admin_can_assign_both_designations(self):
        admin = User.objects.create_user(username="hospital_admin", password="password123")
        admin.profile.designation = UserProfile.Designation.ADMIN
        admin.profile.save()
        self.client.force_login(admin)

        for username, designation in (
            ("new_service", UserProfile.Designation.SERVICE_STAFF),
            ("new_pharmacy", UserProfile.Designation.PHARMACY),
        ):
            response = self.client.post(reverse("admin_doctors"), {
                "first_name": username,
                "last_name": "User",
                "email": "",
                "username": username,
                "designation": designation,
                "specialty": "",
                "password": "password123",
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(User.objects.get(username=username).profile.designation, designation)
