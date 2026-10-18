from django import forms

from .models import Patient


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
    """Full registration-desk patient registration form (mirrors CAMS HMS)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "gender" in self.fields:
            self.fields["gender"].choices = [("", "Select Gender…")] + [
                c for c in self.fields["gender"].choices if c[0] != ""
            ]
        if "marital_status" in self.fields:
            self.fields["marital_status"].choices = [("", "Select Marital Status…")] + [
                c for c in self.fields["marital_status"].choices if c[0] != ""
            ]
        if "payment_type" in self.fields:
            self.fields["payment_type"].choices = [("", "Select Payment Method…")] + [
                c for c in self.fields["payment_type"].choices if c[0] != ""
            ]

    class Meta:
        model = Patient
        fields = [
            # Core identity
            "first_name", "second_name", "last_name",
            "gender", "marital_status",
            "date_of_birth",
            # Contact & address
            "address", "place_panch", "phone", "aadhar_no",
            "abha_no", "care_of",
            # Doctor / email
            "email",
            # Extended address
            "nationality", "state", "district", "pin_code",
            "zone", "block", "religion",
            # Billing
            "reg_fee", "con_fee", "discount", "payment_type",
            # Category
            "patient_category", "reference_no", "valid_upto", "billing_type",
            # Misc
            "mlc",
            # Photo
            "photo",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "valid_upto": forms.DateInput(attrs={"type": "date"}),
            "address": forms.Textarea(attrs={"rows": 2}),
            "gender": forms.Select(),
            "marital_status": forms.Select(),
            "payment_type": forms.Select(),
        }
