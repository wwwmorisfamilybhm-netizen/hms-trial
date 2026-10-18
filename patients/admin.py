from django.contrib import admin
from django.utils import timezone

from appointments.models import Appointment

from .models import Patient, PatientBill, PatientCategoryConfig, RegistrationFeeSettings, RegistrationOption


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


@admin.register(PatientCategoryConfig)
class PatientCategoryConfigAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "registration_fee", "is_active", "display_order"]
    list_editable = ["registration_fee", "is_active", "display_order"]
    search_fields = ["name", "code"]
    ordering = ["display_order", "name"]


@admin.register(RegistrationFeeSettings)
class RegistrationFeeSettingsAdmin(admin.ModelAdmin):
    list_display = ["default_registration_fee", "default_consultation_fee", "default_discount", "updated_at"]
    readonly_fields = ["updated_at"]

    def has_add_permission(self, request):
        return not RegistrationFeeSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.pk = 1
        super().save_model(request, obj, form, change)


@admin.register(RegistrationOption)
class RegistrationOptionAdmin(admin.ModelAdmin):
    list_display = ["name", "option_type", "is_active", "display_order"]
    list_filter = ["option_type", "is_active"]
    list_editable = ["is_active", "display_order"]
    search_fields = ["name"]
    ordering = ["option_type", "display_order", "name"]


@admin.register(PatientBill)
class PatientBillAdmin(admin.ModelAdmin):
    list_display = ["bill_number", "patient", "source", "description", "amount", "discount", "status", "created_at"]
    list_filter = ["source", "status", "created_at"]
    list_editable = ["status"]
    search_fields = ["patient__first_name", "patient__last_name", "patient__mr_number", "description"]
    readonly_fields = ["created_at", "paid_at"]

    def save_model(self, request, obj, form, change):
        if obj.status == PatientBill.Status.PAID and not obj.paid_at:
            obj.paid_at = timezone.now()
        elif obj.status == PatientBill.Status.PENDING:
            obj.paid_at = None
        super().save_model(request, obj, form, change)
