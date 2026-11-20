from django import forms
from django.contrib.auth.models import User

from .models import UserProfile
from patients.models import ServiceDepartment


class StaffAccountForm(forms.ModelForm):
    """Create a staff login account (doctor / OPD / desk / admin) with a photo.

    Used by the admin console to provision new users with an image, name,
    designation, and a login password.
    """

    first_name = forms.CharField(max_length=150, label="First name")
    service_departments = forms.ModelMultipleChoiceField(queryset=ServiceDepartment.objects.all(), required=False, widget=forms.CheckboxSelectMultiple, help_text="Service Staff can access only these departments.")
    last_name = forms.CharField(max_length=150, required=False, label="Last name")
    email = forms.EmailField(required=False, label="Email")
    username = forms.CharField(max_length=150, label="Username")
    designation = forms.ChoiceField(
        choices=UserProfile.Designation.choices,
        label="Designation",
    )
    specialty = forms.ChoiceField(
        choices=UserProfile.SPECIALTIES,
        required=False,
        label="Specialty",
        help_text="Shown under the doctor's name — e.g. Orthopedics, Cardiology.",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        label="Password",
        required=False,
        help_text="Leave blank to keep the current password when editing.",
    )
    photo = forms.ImageField(
        required=False,
        label="Profile photo",
        help_text="Square image works best (JPG or PNG).",
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "username"]

    def __init__(self, *args, **kwargs):
        self.instance_pk = kwargs.get("instance").pk if kwargs.get("instance") else None
        self.editing = self.instance_pk is not None
        super().__init__(*args, **kwargs)

        if self.editing:
            self.fields["username"].help_text = "Leave unchanged to keep the current username."
            # Designation / photo / specialty come from the related UserProfile.
            profile = getattr(self.instance, "profile", None)
            if profile:
                self.fields["designation"].initial = profile.designation
                self.fields["specialty"].initial = profile.specialty
                self.fields["photo"].initial = profile.photo
                self.fields["service_departments"].initial = profile.service_departments.all()

        # Order the visible fields sensibly.
        self.order_fields([
            "photo", "first_name", "last_name", "username",
            "email", "designation", "service_departments", "specialty", "password",
        ])

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        qs = User.objects.filter(username__iexact=username)
        if self.instance_pk:
            qs = qs.exclude(pk=self.instance_pk)
        if qs.exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if not self.editing and not password:
            raise forms.ValidationError("A password is required for a new account.")
        if password and len(password) < 6:
            raise forms.ValidationError("Password must be at least 6 characters.")
        return password

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        elif self.editing:
            # Keep the existing password when the field was left blank.
            user.password = User.objects.get(pk=self.instance_pk).password

        if commit:
            user.save()
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.designation = self.cleaned_data["designation"]
            # Specialty only applies to doctors; clear it for other designations.
            profile.specialty = (
                self.cleaned_data.get("specialty", "")
                if profile.designation == UserProfile.Designation.DOCTOR
                else ""
            )
            photo = self.cleaned_data.get("photo")
            if photo:
                profile.photo = photo
            profile.save()
            profile.service_departments.set(self.cleaned_data.get("service_departments", []) if profile.designation == UserProfile.Designation.SERVICE_STAFF else [])
            self.save_m2m()
        return user
