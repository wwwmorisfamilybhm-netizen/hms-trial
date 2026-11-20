from django.contrib import admin
from django.utils import timezone

from appointments.models import Appointment

from .models import Patient, PatientBill, PatientCategoryConfig, RegistrationFeeSettings, RegistrationOption, AppointmentFeeSettings


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


@admin.register(AppointmentFeeSettings)
class AppointmentFeeSettingsAdmin(admin.ModelAdmin):
    list_display = ["appointment_fee", "consultation_fee", "discount", "updated_at"]
    readonly_fields = ["updated_at"]

    def has_add_permission(self, request):
        return not AppointmentFeeSettings.objects.exists()

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
    search_fields = ["patient__first_name", "patient__last_name", "patient__uhid", "description"]
    readonly_fields = ["created_at", "paid_at"]

    def save_model(self, request, obj, form, change):
        if obj.status == PatientBill.Status.PAID and not obj.paid_at:
            obj.paid_at = timezone.now()
        elif obj.status == PatientBill.Status.PENDING:
            obj.paid_at = None
        super().save_model(request, obj, form, change)


from .models import (
    InitialAssessment, ConsultationSession, PrescriptionItem, ClinicalOrder, DiagnosticReport,
    NurseQueueTicket, NurseTask,
)

@admin.register(InitialAssessment)
class InitialAssessmentAdmin(admin.ModelAdmin):
    list_display = ["patient", "temperature", "pulse", "bp_systolic", "bp_diastolic", "spo2", "created_at"]
    search_fields = ["patient__first_name", "patient__last_name"]


class PrescriptionItemInline(admin.TabularInline):
    readonly_fields = ["dispense_status", "stock_status", "dispensed_quantity", "reserved_quantity", "legacy_dispensed_quantity", "dispensed_by", "released_at", "updated_at"]
    model = PrescriptionItem
    extra = 1


class ClinicalOrderInline(admin.TabularInline):
    readonly_fields = ["status", "result_summary", "operational_details", "assigned_staff", "started_at", "completed_at", "completed_by", "updated_by"]
    model = ClinicalOrder
    extra = 1


@admin.register(ConsultationSession)
class ConsultationSessionAdmin(admin.ModelAdmin):
    list_display = ["id", "patient", "doctor", "visit_type", "status", "diagnosis_primary", "created_at"]
    list_filter = ["status", "visit_type", "created_at"]
    search_fields = ["patient__first_name", "patient__last_name", "diagnosis_primary"]
    inlines = [PrescriptionItemInline, ClinicalOrderInline]


@admin.register(DiagnosticReport)
class DiagnosticReportAdmin(admin.ModelAdmin):
    list_display = ["title", "patient", "category", "status", "report_date"]
    list_filter = ["category", "status", "report_date"]
    search_fields = ["title", "patient__first_name", "patient__last_name"]


@admin.register(NurseQueueTicket)
class NurseQueueTicketAdmin(admin.ModelAdmin):
    list_display = ["ticket_number", "patient", "priority", "status", "sent_by", "claimed_by", "completed_by", "sent_at"]
    list_filter = ["status", "priority", "source", "sent_at"]
    search_fields = ["patient__first_name", "patient__last_name", "patient__uhid"]
    readonly_fields = ["sent_at"]


@admin.register(NurseTask)
class NurseTaskAdmin(admin.ModelAdmin):
    list_display = ["title", "patient", "kind", "priority", "status", "due_at", "assigned_to"]
    list_filter = ["kind", "priority", "status"]
    search_fields = ["title", "patient__first_name", "patient__last_name", "patient__uhid"]



from .models import Medicine, MedicineBatch, StockMovement, PharmacyIssue, PharmacyIssueLine, BatchAllocation, ServiceDepartment, ServiceDefinition


@admin.register(Medicine)
class MedicineAdmin(admin.ModelAdmin):
    list_display = ["name", "strength", "form", "unit", "pack_size", "reorder_level", "is_active"]
    search_fields = ["name", "generic_name"]

    def get_readonly_fields(self, request, obj=None):
        return ["name", "strength", "form", "unit", "pack_size"] if obj and obj.batches.exists() else []


class OperationsReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MedicineBatch)
class MedicineBatchAdmin(OperationsReadOnlyAdmin):
    list_display = ["medicine", "batch_number", "expiry_date", "on_hand", "reserved", "selling_price"]
    search_fields = ["medicine__name", "batch_number"]


@admin.register(StockMovement)
class StockMovementAdmin(OperationsReadOnlyAdmin):
    list_display = ["batch", "quantity", "kind", "reason", "actor", "created_at"]
    list_filter = ["kind", "created_at"]


@admin.register(PharmacyIssue)
class PharmacyIssueAdmin(OperationsReadOnlyAdmin):
    list_display = ["id", "consultation", "bill", "status", "created_at", "dispensed_at"]


admin.site.register(ServiceDepartment)
admin.site.register(ServiceDefinition)
