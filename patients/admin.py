from django.contrib import admin

from appointments.models import Appointment

from .models import Patient


class AppointmentInline(admin.TabularInline):
    model = Appointment
    extra = 0
    fields = ["doctor", "date", "start_time", "duration_minutes", "status", "reason"]
    readonly_fields = ["created_at"]


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ["full_name", "date_of_birth", "gender", "blood_group", "phone", "created_at"]
    list_filter = ["gender", "blood_group", "created_at"]
    search_fields = ["first_name", "last_name", "phone", "email"]
    inlines = [AppointmentInline]
