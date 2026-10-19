from django.contrib import admin

from .models import Doctor, Appointment


@admin.register(Doctor)
class DoctorAdmin(admin.ModelAdmin):
    list_display = ["full_name", "specialty", "phone", "email"]
    list_filter = ["specialty"]
    search_fields = ["first_name", "last_name", "email"]


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ["patient", "doctor", "date", "start_time", "duration_minutes", "status"]
    list_filter = ["status", "date", "doctor"]
    search_fields = ["patient__first_name", "patient__last_name", "doctor__last_name", "reason"]
    date_hierarchy = "date"
