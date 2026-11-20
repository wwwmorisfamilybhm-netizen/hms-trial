from datetime import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import UserProfile
from core.operations import (
    appointment_pre_visit_pending,
    pay_bills,
    sync_service_bills,
)
from patients.models import (
    ClinicalOrder,
    ConsultationSession,
    InitialAssessment,
    Patient,
    PatientBill,
    ServiceDefinition,
    ServiceDepartment,
)

User = get_user_model()


class BillingFlowTests(TestCase):
    def setUp(self):
        self.registration = User.objects.create_user(
            username="flow_reg", password="password123"
        )
        self.registration.profile.designation = UserProfile.Designation.REGISTRATION_DESK
        self.registration.profile.save()

        self.opd = User.objects.create_user(
            username="flow_opd", password="password123"
        )
        self.opd.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        self.opd.profile.save()

        self.doctor = User.objects.create_user(
            username="flow_doc", password="password123", first_name="Flow"
        )
        self.doctor.profile.designation = UserProfile.Designation.DOCTOR
        self.doctor.profile.specialty = "GEN"
        self.doctor.profile.save()

        self.service_dept = ServiceDepartment.objects.create(
            slug="flow-lab", name="Flow Laboratory"
        )
        self.service_user = User.objects.create_user(
            username="flow_service", password="password123"
        )
        self.service_user.profile.designation = UserProfile.Designation.SERVICE_STAFF
        self.service_user.profile.save()
        self.service_user.profile.service_departments.add(self.service_dept)

        self.service_def = ServiceDefinition.objects.create(
            name="Flow CBC",
            department=self.service_dept,
            price=Decimal("300.00"),
        )

        self.patient = Patient.objects.create(
            first_name="Flow",
            last_name="Patient",
            phone="9000000000",
            gender=Patient.Gender.FEMALE,
        )
        from appointments.models import Appointment

        self.appointment = Appointment.objects.create(
            patient=self.patient,
            doctor=None,
            date=timezone.localdate(),
            start_time=time(10, 0),
            duration_minutes=30,
            reason="Flow visit",
        )
        self.registration_bill = PatientBill.objects.create(
            patient=self.patient,
            source=PatientBill.Source.REGISTRATION,
            appointment=self.appointment,
            description="Registration & consultation",
            amount=Decimal("100.00"),
            created_by=self.registration,
        )
        self.client = Client()

    def _login(self, user):
        self.client.force_login(user)

    def test_pre_visit_payment_gate_and_payment_metadata(self):
        self.assertTrue(appointment_pre_visit_pending(self.appointment))
        self._login(self.registration)
        group, paid = pay_bills(
            user=self.registration,
            bills=[self.registration_bill],
            payment_method=PatientBill.PaymentMethod.UPI,
            payment_reference="UPI-123",
        )
        self.registration_bill.refresh_from_db()
        self.assertEqual(self.registration_bill.status, PatientBill.Status.PAID)
        self.assertEqual(self.registration_bill.paid_by, self.registration)
        self.assertEqual(self.registration_bill.payment_method, "upi")
        self.assertEqual(self.registration_bill.payment_reference, "UPI-123")
        self.assertEqual(self.registration_bill.payment_group, group)
        self.assertEqual(len(paid), 1)
        self.assertFalse(appointment_pre_visit_pending(self.appointment))

    def test_opd_flow_shows_payment_then_assessment(self):
        from core.views import _opd_patient_flow

        rows = _opd_patient_flow([self.patient])
        self.assertEqual(rows[0]["status"], "Awaiting Payment")
        self.assertFalse(rows[0]["pre_visit_paid"])

        self._login(self.registration)
        pay_bills(user=self.registration, bills=[self.registration_bill])
        rows = _opd_patient_flow([self.patient])
        self.assertEqual(rows[0]["status"], "Assessment")
        self.assertTrue(rows[0]["pre_visit_paid"])

    def test_assessment_blocked_until_paid(self):
        self._login(self.opd)
        response = self.client.post(
            reverse("opd_send_to_nurse"),
            {
                "patient_id": self.patient.pk,
                "appointment_id": self.appointment.pk,
                "priority": "routine",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.patient.nurse_tickets.exists())

        self._login(self.registration)
        pay_bills(user=self.registration, bills=[self.registration_bill])

        self._login(self.opd)
        response = self.client.post(
            reverse("opd_send_to_nurse"),
            {
                "patient_id": self.patient.pk,
                "appointment_id": self.appointment.pk,
                "priority": "routine",
            },
        )
        self.assertEqual(response.status_code, 302)
        ticket = self.patient.nurse_tickets.get()
        self.assertEqual(ticket.appointment_id, self.appointment.pk)

    def test_doctor_allocation_requires_assessment(self):
        self._login(self.registration)
        pay_bills(user=self.registration, bills=[self.registration_bill])

        self._login(self.opd)
        response = self.client.post(
            reverse("assign_doctor", args=[self.patient.pk]),
            {"doctor_id": self.doctor.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.patient.refresh_from_db()
        self.assertIsNone(self.patient.assigned_doctor)

        InitialAssessment.objects.create(
            patient=self.patient,
            status=InitialAssessment.Status.COMPLETED,
            completed_at=timezone.now(),
            pulse=80,
            complaints="Fever",
        )
        response = self.client.post(
            reverse("assign_doctor", args=[self.patient.pk]),
            {"doctor_id": self.doctor.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor, self.doctor)

    def test_service_bill_created_and_released_after_payment(self):
        consultation = ConsultationSession.objects.create(
            patient=self.patient,
            appointment=self.appointment,
            doctor=self.doctor,
            visit_token="FLOW-001",
            status=ConsultationSession.Status.COMPLETED,
        )
        order = ClinicalOrder.objects.create(
            consultation=consultation,
            order_type=ClinicalOrder.OrderType.LABORATORY,
            name=self.service_def.name,
            department=self.service_dept,
            service_definition=self.service_def,
            status="Ordered",
        )
        created = sync_service_bills(user=self.doctor, consultation=consultation)
        self.assertEqual(len(created), 1)
        order.refresh_from_db()
        self.assertIsNotNone(order.bill_id)
        self.assertEqual(order.bill.source, PatientBill.Source.SERVICE)
        self.assertEqual(order.bill.amount, Decimal("300.00"))
        self.assertEqual(order.bill.consultation, consultation)

        # Service staff do not see the order until the bill is paid.
        self._login(self.service_user)
        response = self.client.get(
            reverse("opd_service_queue", args=[self.service_dept.slug])
        )
        self.assertNotContains(response, "Flow CBC")

        self._login(self.registration)
        pay_bills(user=self.registration, bills=[order.bill])

        self._login(self.service_user)
        response = self.client.get(
            reverse("opd_service_queue", args=[self.service_dept.slug])
        )
        self.assertContains(response, "Flow CBC")

    def test_post_consultation_queue_and_pay_all(self):
        consultation = ConsultationSession.objects.create(
            patient=self.patient,
            appointment=self.appointment,
            doctor=self.doctor,
            visit_token="FLOW-002",
            status=ConsultationSession.Status.COMPLETED,
        )
        order = ClinicalOrder.objects.create(
            consultation=consultation,
            order_type=ClinicalOrder.OrderType.LABORATORY,
            name=self.service_def.name,
            department=self.service_dept,
            service_definition=self.service_def,
            status="Ordered",
        )
        sync_service_bills(user=self.doctor, consultation=consultation)

        self._login(self.registration)
        response = self.client.get(reverse("post_consultation_billing"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Flow Patient")

        response = self.client.get(reverse("visit_billing", args=[consultation.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Flow CBC")

        response = self.client.post(
            reverse("visit_billing_pay", args=[consultation.pk]),
            {"payment_method": "cash", "payment_reference": "CASH-1"},
        )
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.bill.status, PatientBill.Status.PAID)
        self.assertEqual(order.bill.payment_reference, "CASH-1")

        response = self.client.get(reverse("visit_billing_slip", args=[consultation.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PAYMENT RECEIPT")
