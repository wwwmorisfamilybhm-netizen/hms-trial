from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.models import UserProfile


class AuthenticationAccessTests(TestCase):
    def test_anonymous_requests_require_login(self):
        routes = [
            ("opd_assistant_dashboard", (), "get"),
            ("registration_desk", (), "get"),
            ("patient_queue", (), "get"),
            ("patient_list", (), "get"),
            ("patient_detail", (1,), "get"),
            ("appointment_list", (), "get"),
            ("service_staff_dashboard", (), "get"),
            ("pharmacy_dashboard", (), "get"),
            ("report_download", (1,), "get"),
            ("save_consultation", (), "post"),
            ("save_initial_assessment", (), "post"),
        ]
        for name, args, method in routes:
            with self.subTest(route=name):
                url = reverse(name, args=args)
                response = getattr(self.client, method)(url)
                self.assertRedirects(response, f"{reverse('login')}?next={url}", fetch_redirect_response=False)

    def test_browser_session_and_logout(self):
        user = get_user_model().objects.create_user(username="opd_session", password="test-password")
        user.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        user.profile.save()

        response = self.client.post(reverse("login"), {
            "username": user.username,
            "password": "test-password",
            "designation": UserProfile.Designation.OPD_ASSISTANT,
        })
        self.assertRedirects(response, reverse("opd_assistant_dashboard"))
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertFalse(cookie["expires"])
        self.assertFalse(cookie["max-age"])
        self.assertEqual(self.client.get(reverse("opd_assistant_dashboard")).status_code, 200)
        self.assertRedirects(self.client.get(reverse("logout")), reverse("login"))
        self.assertEqual(self.client.get(reverse("opd_assistant_dashboard")).status_code, 302)
        self.assertEqual(Client().get(reverse("opd_assistant_dashboard")).status_code, 302)

    def test_direct_browser_navigation_logs_out_existing_session(self):
        user = get_user_model().objects.create_user(username="opd_direct", password="test-password")
        user.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        user.profile.save()
        headers = {
            "HTTP_SEC_FETCH_MODE": "navigate",
            "HTTP_SEC_FETCH_DEST": "document",
            "HTTP_SEC_FETCH_SITE": "none",
        }
        for name in ("opd_assistant_dashboard", "patient_list", "appointment_list", "login"):
            with self.subTest(route=name):
                self.client.force_login(user)
                response = self.client.get(reverse(name), **headers)
                self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_internal_navigation_keeps_session(self):
        user = get_user_model().objects.create_user(username="opd_internal", password="test-password")
        user.profile.designation = UserProfile.Designation.OPD_ASSISTANT
        user.profile.save()
        self.client.force_login(user)
        response = self.client.get(reverse("opd_assistant_dashboard"),
                                   HTTP_SEC_FETCH_MODE="navigate", HTTP_SEC_FETCH_DEST="document",
                                   HTTP_SEC_FETCH_SITE="same-origin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("_auth_user_id", self.client.session)
