from datetime import datetime, timedelta

from django import forms

from patients.models import Patient

from .models import Appointment, Doctor


class AppointmentForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = [
            "patient", "doctor", "date", "start_time",
            "duration_minutes", "reason",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Show patient names nicely in the dropdown, newest first
        self.fields["patient"].queryset = Patient.objects.order_by("last_name")
        self.fields["patient"].label_from_instance = (
            lambda obj: f"{obj.full_name} ({obj.phone or 'no phone'})"
        )
        self.fields["duration_minutes"].initial = 30

    def clean(self):
        cleaned = super().clean()
        doctor = cleaned.get("doctor")
        date = cleaned.get("date")
        start_time = cleaned.get("start_time")
        duration = cleaned.get("duration_minutes") or 30

        if not (doctor and date and start_time):
            return cleaned

        # Check this slot doesn't overlap another appointment of the same doctor
        new_start = datetime.combine(date, start_time)
        new_end = new_start + timedelta(minutes=duration)

        same_day = Appointment.objects.filter(
            doctor=doctor, date=date
        ).exclude(pk=self.instance.pk)

        for appt in same_day:
            other_start = datetime.combine(date, appt.start_time)
            other_end = other_start + timedelta(minutes=appt.duration_minutes)
            if new_start < other_end and other_start < new_end:
                raise forms.ValidationError(
                    f"Dr. {doctor.full_name} is already booked from "
                    f"{appt.start_time:%H:%M}–{appt.end_time:%H:%M} on {date}."
                )
        return cleaned
