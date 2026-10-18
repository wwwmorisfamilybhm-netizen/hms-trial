from datetime import date

from django.test import TestCase

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
