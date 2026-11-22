from datetime import date
from io import BytesIO
from tempfile import TemporaryDirectory

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .forms import (
    COMMON_RELIGIONS,
    INDIAN_STATES_AND_UNION_TERRITORIES,
    PatientRegistrationForm,
)
from .models import Patient, PatientCategoryConfig, RegistrationFeeSettings


class PatientRegistrationFormTests(TestCase):
    def setUp(self):
        PatientCategoryConfig.objects.update_or_create(code="GENERAL", defaults={"name": "General", "is_active": True, "registration_fee": None})
        RegistrationFeeSettings.objects.update_or_create(pk=1, defaults={"default_registration_fee": 50})

    def form_data(self, **overrides):
        data = {
            "first_name": "Test",
            "last_name": "",
            "phone": "9876543210",
            "patient_category": "GENERAL",
            "payment_type": "cash",
            "billing_type": "Individual",
        }
        data.update(overrides)
        return data

    def test_category_uses_current_configured_default_and_normalizes_phone(self):
        form = PatientRegistrationForm(data=self.form_data(reg_fee="99999"))
        self.assertTrue(form.is_valid(), form.errors)
        patient = form.save()
        self.assertEqual(patient.reg_fee, 50)
        self.assertEqual(patient.phone, "+919876543210")
        self.assertEqual(patient.last_name, "")

    def test_category_fee_overrides_default(self):
        PatientCategoryConfig.objects.update_or_create(code="BPL", defaults={"name": "BPL", "registration_fee": 12, "is_active": True})
        form = PatientRegistrationForm(data=self.form_data(patient_category="BPL"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().reg_fee, 12)

    def test_invalid_mobile_number_is_rejected(self):
        form = PatientRegistrationForm(data=self.form_data(phone="12345"))
        self.assertFalse(form.is_valid())
        self.assertIn("phone", form.errors)

    def test_age_without_dob_calculates_date_of_birth(self):
        today = date.today()
        form = PatientRegistrationForm(data=self.form_data(age_years="20", age_months="0", age_days="0"))
        self.assertTrue(form.is_valid(), form.errors)
        patient = form.save()
        self.assertEqual(patient.date_of_birth.year, today.year - 20)

    def test_conflicting_dob_and_age_are_rejected(self):
        form = PatientRegistrationForm(data=self.form_data(date_of_birth="2000-01-01", age_years="10", age_months="0", age_days="0"))
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_missing_fee_configuration_blocks_registration(self):
        RegistrationFeeSettings.objects.all().delete()
        form = PatientRegistrationForm(data=self.form_data())
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_edit_keeps_the_fee_snapshot_saved_on_patient(self):
        patient = Patient.objects.create(first_name="Existing", phone="+919876543210", reg_fee=23, patient_category="GENERAL")
        RegistrationFeeSettings.objects.update_or_create(pk=1, defaults={"default_registration_fee": 75})
        form = PatientRegistrationForm(data=self.form_data(first_name="Existing"), instance=patient)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().reg_fee, 23)

    def test_state_and_religion_dropdowns_have_requested_defaults(self):
        form = PatientRegistrationForm()
        state_choices = [value for value, _label in form.fields["state"].choices]
        religion_choices = [value for value, _label in form.fields["religion"].choices]
        self.assertEqual(form.fields["state"].initial, "Bihar")
        self.assertEqual(state_choices[1:], INDIAN_STATES_AND_UNION_TERRITORIES)
        self.assertEqual(religion_choices[1:5], COMMON_RELIGIONS)

    def test_entry_fields_opt_out_of_browser_autofill(self):
        """The operator's saved name/address details must never land in a patient record.

        Chrome ignores autocomplete="off" for its address autofill profile and
        silently filled the operator's own last name ("Mayer") into the patient's
        Last Name field. Every entry field opts out so autofill has nothing to
        match on, and the rendered form carries the autofill guard.
        """
        form = PatientRegistrationForm()
        for name in (
            "first_name", "second_name", "last_name", "phone", "email", "address",
            "care_of", "aadhar_no", "abha_no", "pin_code", "district", "village",
            "block", "zone", "reference_no",
        ):
            with self.subTest(field=name):
                self.assertEqual(form.fields[name].widget.attrs.get("autocomplete"), "off")


@override_settings(STORAGES={
    **settings.STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class PatientEditViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="patient-editor")
        cls.user.profile.designation = "admin"
        cls.user.profile.save()
        cls.patient = Patient.objects.create(
            first_name="Sam",
            second_name="Lee",
            last_name="Rao",
            uhid="100002",
            phone="+919876543210",
            date_of_birth=date(2000, 8, 23),
            patient_category="LEGACY",
            reg_fee=23,
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.edit_url = reverse("patient_edit", args=[self.patient.pk])
        self.detail_url = reverse("patient_detail", args=[self.patient.pk])

    def edit_data(self, **overrides):
        return {
            "first_name": "Sam",
            "second_name": "Lee",
            "last_name": "Rao",
            "phone": "9876543210",
            "date_of_birth": "2000-08-23",
            "patient_category": "LEGACY",
            "payment_type": "cash",
            "billing_type": "Individual",
            **overrides,
        }

    def test_editor_renders_each_field_and_the_saved_identity(self):
        response = self.client.get(self.edit_url)

        self.assertTemplateUsed(response, "patients/patient_edit.html")
        self.assertContains(response, "Sam Lee Rao")
        self.assertContains(response, "UHID #100002")
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, 'value="2000-08-23"')
        self.assertContains(response, self.detail_url)
        self.assertContains(response, reverse("patient_delete", args=[self.patient.pk]))
        form = response.context["form"]
        for field in form.visible_fields():
            self.assertContains(response, f'id="{field.id_for_label}"', count=1)
        for name in ("age_years", "age_months", "age_days"):
            self.assertIsNone(form[name].value())
        self.assertIn(("LEGACY", "LEGACY"), form.fields["patient_category"].choices)

    def test_updates_date_and_category_without_recalculating_the_fee(self):
        response = self.client.post(
            self.edit_url,
            self.edit_data(
                date_of_birth="2001-05-15",
                patient_category="BPL",
                email="patient@example.com",
                reg_fee="99999",
            ),
            follow=True,
        )

        self.assertRedirects(response, self.detail_url)
        self.assertContains(response, "Patient Sam Rao updated.")
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.date_of_birth, date(2001, 5, 15))
        self.assertEqual(self.patient.patient_category, "BPL")
        self.assertEqual(self.patient.email, "patient@example.com")
        self.assertEqual(self.patient.reg_fee, 23)

    def test_partial_emergency_record_accepts_age_without_date_of_birth(self):
        patient = Patient.objects.create(first_name="Emergency", is_emergency=True)
        url = reverse("patient_edit", args=[patient.pk])
        self.assertContains(self.client.get(url), f"Patient #{patient.pk}")
        response = self.client.post(url, self.edit_data(
            first_name=patient.first_name,
            second_name="",
            last_name="",
            date_of_birth="",
            age_years="20",
            patient_category="GENERAL",
        ))

        self.assertRedirects(response, reverse("patient_detail", args=[patient.pk]))
        patient.refresh_from_db()
        self.assertEqual(patient.date_of_birth.year, timezone.localdate().year - 20)
        self.assertEqual(patient.phone, "+919876543210")

    def test_detail_renders_unassigned_open_task(self):
        """An open nurse task without an assignee must not crash patient detail."""
        from .models import NurseTask

        NurseTask.objects.create(
            patient=self.patient, title="Dressing change", status="pending"
        )
        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dressing change")
        self.assertContains(response, "Unassigned")

    def test_invalid_submission_shows_linked_errors_and_retains_values(self):
        response = self.client.post(
            self.edit_url, self.edit_data(first_name="Draft", phone="123", age_years="2")
        )

        self.assertContains(response, 'href="#id_phone"')
        self.assertContains(response, 'aria-describedby="id_phone_error"')
        self.assertContains(response, 'id="id_phone_error"')
        self.assertContains(response, "Date of birth and age do not match")
        self.assertContains(response, 'value="Draft"')
        self.assertContains(response, "Sam Lee Rao")
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.first_name, "Sam")

    def test_photo_can_be_replaced_and_cleared(self):
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            for name in ("original.png", "replacement.png"):
                image = BytesIO()
                Image.new("RGB", (2, 2)).save(image, format="PNG")
                upload = SimpleUploadedFile(name, image.getvalue(), content_type="image/png")
                response = self.client.post(self.edit_url, self.edit_data(photo=upload))
                self.assertRedirects(response, self.detail_url)
                self.patient.refresh_from_db()
                self.assertEqual(self.patient.photo.name, f"patient_photos/{name}")

            response = self.client.get(self.edit_url)
            self.assertContains(response, self.patient.photo.url)
            self.assertContains(response, 'name="photo-clear"')
            response = self.client.post(self.edit_url, self.edit_data(**{"photo-clear": "on"}))
            self.assertRedirects(response, self.detail_url)
            self.patient.refresh_from_db()
            self.assertFalse(self.patient.photo)

    def test_invalid_photo_shows_error_without_saving(self):
        upload = SimpleUploadedFile("invalid.png", b"invalid image", content_type="image/png")
        response = self.client.post(self.edit_url, self.edit_data(photo=upload))

        self.assertContains(response, 'href="#id_photo"')
        self.assertContains(response, "Choose the photo again before saving.")
        self.patient.refresh_from_db()
        self.assertFalse(self.patient.photo)

    def test_new_registration_routes_use_their_original_templates(self):
        for route, template in (
            ("patient_create", "patients/patient_form.html"),
            ("registration_desk_new", "core/registration_desk_form.html"),
        ):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertTemplateUsed(response, template)
                self.assertNotContains(response, "patient_edit.css")
