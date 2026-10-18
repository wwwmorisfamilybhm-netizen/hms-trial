import calendar
import re
from datetime import date
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    Patient, PatientBill, PatientCategoryConfig, RegistrationFeeSettings, RegistrationOption,
)


INDIAN_STATES_AND_UNION_TERRITORIES = [
    "Andaman and Nicobar Islands",
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chandigarh",
    "Chhattisgarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jammu and Kashmir",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Ladakh",
    "Lakshadweep",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Puducherry",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
]
COMMON_RELIGIONS = ["Hindu", "Muslim", "Christian", "Other"]


class PatientForm(forms.ModelForm):
    """Simple edit form used in the main patients app."""
    class Meta:
        model = Patient
        fields = [
            "first_name", "last_name", "date_of_birth", "gender",
            "blood_group", "phone", "email", "address", "allergies",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "address": forms.Textarea(attrs={"rows": 3}),
            "allergies": forms.Textarea(attrs={"rows": 2}),
        }


class PatientRegistrationForm(forms.ModelForm):
    """Registration form with database-backed options and server-calculated fees."""
    age_years = forms.IntegerField(required=False, min_value=0, widget=forms.NumberInput(attrs={"min": 0}))
    age_months = forms.IntegerField(required=False, min_value=0, max_value=11, widget=forms.NumberInput(attrs={"min": 0, "max": 11}))
    age_days = forms.IntegerField(required=False, min_value=0, max_value=30, widget=forms.NumberInput(attrs={"min": 0, "max": 30}))
    reg_fee = forms.DecimalField(required=False, disabled=True, max_digits=8, decimal_places=2)

    class Meta:
        model = Patient
        fields = [
            "first_name", "second_name", "last_name", "gender", "marital_status",
            "centre", "date_of_birth", "phone", "email", "care_of", "aadhar_no", "abha_no",
            "nationality", "state", "district", "pin_code", "zone", "block", "village",
            "religion", "address", "reg_fee", "con_fee", "discount", "payment_type",
            "patient_category", "reference_no", "billing_type", "mlc", "photo",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "address": forms.Textarea(attrs={"rows": 2}),
            "gender": forms.Select(),
            "marital_status": forms.Select(),
            "payment_type": forms.Select(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = False
        self.fields["phone"].required = True
        self.fields["con_fee"].required = False
        self.fields["discount"].required = False
        for name, prompt in (("gender", "Select Gender…"), ("marital_status", "Select Marital Status…"), ("payment_type", "Select Payment Method…")):
            choices = list(self.fields[name].choices)
            self.fields[name].choices = [("", prompt)] + [(key, label) for key, label in choices if key]

        for name, kind, standard_options in (
            ("state", RegistrationOption.OptionType.STATE, INDIAN_STATES_AND_UNION_TERRITORIES),
            ("religion", RegistrationOption.OptionType.RELIGION, COMMON_RELIGIONS),
            ("centre", RegistrationOption.OptionType.CENTRE, []),
        ):
            options = list(standard_options)
            options.extend(
                option for option in RegistrationOption.objects.filter(
                    option_type=kind, is_active=True
                ).values_list("name", flat=True)
                if option not in options
            )
            current = getattr(self.instance, name, "") if self.instance.pk else ""
            if current and current not in options:
                options.append(current)
            self.fields[name] = forms.ChoiceField(
                required=False,
                choices=[("", f"Select {name.title()}…")] + [(option, option) for option in options],
                initial=("Bihar" if name == "state" and not self.instance.pk else current),
            )

        categories = list(PatientCategoryConfig.objects.filter(is_active=True))
        current_code = self.instance.patient_category if self.instance.pk else ""
        if current_code and not any(c.code == current_code for c in categories):
            current_config = PatientCategoryConfig.objects.filter(code=current_code).first()
            if current_config:
                categories.append(current_config)
        self.fields["patient_category"] = forms.ChoiceField(
            required=False,
            choices=[("", "Select Category…")] + [(c.code, c.name) for c in categories],
            initial=current_code or "GENERAL",
        )
        self.category_fees = {c.code: c.registration_fee for c in categories}
        self.default_fee = RegistrationFeeSettings.objects.order_by("pk").values_list("default_registration_fee", flat=True).first()
        self.default_consultation_fee = (
            RegistrationFeeSettings.objects.order_by("pk").values_list("default_consultation_fee", flat=True).first()
        )
        self.default_discount = (
            RegistrationFeeSettings.objects.order_by("pk").values_list("default_discount", flat=True).first()
        )
        selected = (self.data.get("patient_category") if self.is_bound else current_code) or "GENERAL"
        self.fields["reg_fee"].initial = self.instance.reg_fee if self.instance.pk else self._fee_for(selected)
        if not self.is_bound and not self.instance.pk:
            self.fields["con_fee"].initial = self.default_consultation_fee or Decimal("0")
            self.fields["discount"].initial = self.default_discount or Decimal("0")
        if not self.is_bound and self.instance.pk and self.instance.date_of_birth:
            age = self._age_parts(self.instance.date_of_birth, timezone.localdate())
            for name, value in zip(("age_years", "age_months", "age_days"), age):
                self.fields[name].initial = value

    def _fee_for(self, code):
        category_fee = self.category_fees.get(code)
        return category_fee if category_fee is not None else self.default_fee

    @staticmethod
    def _age_parts(birth, today):
        years = today.year - birth.year
        months = today.month - birth.month
        days = today.day - birth.day
        if days < 0:
            months -= 1
            previous_month = today.month - 1 or 12
            previous_year = today.year if today.month > 1 else today.year - 1
            days += calendar.monthrange(previous_year, previous_month)[1]
        if months < 0:
            years -= 1
            months += 12
        return years, months, days

    @staticmethod
    def _date_from_age(today, years, months, days):
        month_index = today.year * 12 + today.month - 1 - years * 12 - months
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        result = date(year, month, day)
        try:
            from datetime import timedelta
            return result - timedelta(days=days)
        except OverflowError as exc:
            raise ValidationError("Age is outside the supported date range.") from exc

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        digits = re.sub(r"[\s().-]", "", phone)
        if digits.startswith("+91"):
            digits = digits[3:]
        elif digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        if not re.fullmatch(r"[6-9]\d{9}", digits):
            raise ValidationError("Enter a valid 10-digit Indian mobile number.")
        return "+91" + digits

    def clean(self):
        cleaned = super().clean()
        dob = cleaned.get("date_of_birth")
        today = timezone.localdate()
        if dob and dob > today:
            self.add_error("date_of_birth", "Date of birth cannot be in the future.")
        parts = [cleaned.get("age_years"), cleaned.get("age_months"), cleaned.get("age_days")]
        age_entered = any(value is not None for value in parts)
        if dob and age_entered:
            entered = tuple(value or 0 for value in parts)
            if entered != self._age_parts(dob, today):
                self.add_error(None, "Date of birth and age do not match. Correct either the date of birth or age fields.")
        elif not dob and age_entered:
            try:
                cleaned["date_of_birth"] = self._date_from_age(today, *(value or 0 for value in parts))
            except (ValueError, ValidationError) as exc:
                self.add_error("date_of_birth", str(exc))

        code = cleaned.get("patient_category") or ""
        # Keep the fee snapshot that was actually charged on existing records.
        fee = self.instance.reg_fee if self.instance.pk else self._fee_for(code)
        if fee is None:
            self.add_error(None, "Registration fee is not configured for this category. Ask an administrator to configure a category fee or default fee.")
        cleaned["reg_fee"] = fee
        con_fee = cleaned.get("con_fee")
        cleaned["con_fee"] = con_fee if con_fee is not None else (self.default_consultation_fee or Decimal("0"))
        discount = cleaned.get("discount")
        cleaned["discount"] = discount if discount is not None else (self.default_discount or Decimal("0"))
        if cleaned["discount"] < Decimal("0"):
            self.add_error("discount", "Discount cannot be negative.")
        if cleaned.get("con_fee") is not None and cleaned["con_fee"] < Decimal("0"):
            self.add_error("con_fee", "Consultation fee cannot be negative.")
        return cleaned

    def save(self, commit=True):
        patient = super().save(commit=False)
        patient.reg_fee = self.cleaned_data["reg_fee"]
        if commit:
            patient.save()
            self.save_m2m()
        return patient


class EmergencyRegistrationForm(forms.Form):
    """Fast emergency intake form.

    Captures only the essentials (name, age, sex, MR number) so the patient can
    be seen immediately. The remaining registration details are completed later
    by editing the record from History. Payment is always left pending and is
    collected afterwards from the Bill Command Center.
    """

    name = forms.CharField(max_length=200, label="Patient Name")
    age_years = forms.IntegerField(min_value=0, max_value=130, label="Age (Years)")
    gender = forms.ChoiceField(choices=Patient.Gender.choices, label="Sex")
    mr_number = forms.CharField(max_length=20, label="MR Number", required=False)

    def __init__(self, *args, **kwargs):
        next_mr = kwargs.pop("next_mr", None)
        super().__init__(*args, **kwargs)
        self.fields["gender"].choices = [("", "Select Sex…")] + list(Patient.Gender.choices)
        if next_mr and not self.is_bound:
            self.fields["mr_number"].initial = str(next_mr)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise ValidationError("Enter the patient's name.")
        return name

    def clean_mr_number(self):
        mr_number = self.cleaned_data["mr_number"].strip()
        if mr_number and Patient.objects.filter(mr_number=mr_number).exists():
            raise ValidationError("This MR number is already assigned to another patient.")
        return mr_number

    def clean(self):
        cleaned = super().clean()
        age_years = cleaned.get("age_years")
        if age_years is not None:
            today = timezone.localdate()
            try:
                cleaned["date_of_birth"] = today.replace(year=today.year - age_years)
            except ValueError:
                # 29 Feb birthday landing on a non-leap year.
                cleaned["date_of_birth"] = today.replace(year=today.year - age_years, day=28)
        return cleaned


class PatientBillForm(forms.ModelForm):
    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=10, decimal_places=2)
    discount = forms.DecimalField(min_value=Decimal("0"), required=False, max_digits=10, decimal_places=2, initial=0)

    class Meta:
        model = PatientBill
        fields = ["description", "amount", "discount"]
        widgets = {"description": forms.TextInput(attrs={"maxlength": 200})}

    def clean_discount(self):
        return self.cleaned_data.get("discount") or Decimal("0")

    def clean(self):
        cleaned = super().clean()
        amount = cleaned.get("amount")
        discount = cleaned.get("discount") or Decimal("0")
        if amount is not None and discount > amount:
            self.add_error("discount", "Discount cannot exceed the bill amount.")
        return cleaned
