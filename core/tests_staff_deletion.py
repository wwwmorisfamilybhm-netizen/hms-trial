from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import UserProfile
from patients.models import ConsultationSession, Patient, PatientBill, PharmacyIssue


class StaffDeletionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="admin_one")
        self.admin.profile.designation = UserProfile.Designation.ADMIN
        self.admin.profile.save()
        self.other_admin = User.objects.create_user(username="admin_two")
        self.other_admin.profile.designation = UserProfile.Designation.ADMIN
        self.other_admin.profile.save()
        self.superuser = User.objects.create_superuser(username="superuser", password="testpass")
        self.staff = User.objects.create_user(username="staff_one")
        self.url = reverse("admin_staff_delete", args=[self.staff.pk])

    def test_admin_can_confirm_and_delete_staff(self):
        self.client.force_login(self.admin)
        roster = self.client.get(reverse("admin_doctors"))
        self.assertContains(roster, self.url)
        self.assertNotContains(roster, reverse("admin_staff_delete", args=[self.other_admin.pk]))
        self.assertNotContains(roster, reverse("admin_staff_delete", args=[self.superuser.pk]))
        self.assertContains(self.client.get(self.url), "Delete staff account?")
        self.assertTrue(get_user_model().objects.filter(pk=self.staff.pk).exists())
        self.assertRedirects(self.client.post(self.url), reverse("admin_doctors"))
        self.assertFalse(get_user_model().objects.filter(pk=self.staff.pk).exists())

    def test_admins_and_superusers_cannot_be_deleted(self):
        self.client.force_login(self.admin)
        for user in (self.admin, self.other_admin, self.superuser):
            url = reverse("admin_staff_delete", args=[user.pk])
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url).status_code, 403)
            self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())

    def test_non_admin_cannot_delete(self):
        self.client.force_login(self.staff)
        other = get_user_model().objects.create_user(username="other_staff")
        self.client.post(reverse("admin_staff_delete", args=[other.pk]))
        self.assertTrue(get_user_model().objects.filter(pk=other.pk).exists())

    def test_protected_records_block_deletion(self):
        patient = Patient.objects.create(first_name="Test")
        consultation = ConsultationSession.objects.create(patient=patient)
        bill = PatientBill.objects.create(patient=patient, consultation=consultation, amount=10)
        PharmacyIssue.objects.create(consultation=consultation, bill=bill, created_by=self.staff, request_key=uuid4())
        self.client.force_login(self.admin)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "protected records")
        self.assertTrue(get_user_model().objects.filter(pk=self.staff.pk).exists())
