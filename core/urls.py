from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("registration-desk/", views.registration_desk, name="registration_desk"),
    path("registration-desk/emergency/", views.emergency_registration, name="emergency_registration"),
    path("registration-desk/billing/command-center/", views.bill_command_center, name="bill_command_center"),
    path("registration-desk/billing/bill/<int:bill_id>/print/", views.patient_bill_slip, name="patient_bill_slip"),
    path("registration-desk/billing/bill/<int:bill_id>/status/", views.bill_set_status, name="bill_set_status"),
    path("registration-desk/billing/bill/<int:bill_id>/whatsapp/", views.bill_whatsapp, name="bill_whatsapp"),
    path("registration-desk/billing/pharmacy/", views.billing_console_placeholder, {"console_name": "pharmacy", "title": "Pharmacy Billing"}, name="pharmacy_billing"),
    path("registration-desk/billing/lab/", views.billing_console_placeholder, {"console_name": "lab", "title": "Lab Billing"}, name="lab_billing"),
    path("registration-desk/billing/general/", views.billing_console_placeholder, {"console_name": "general", "title": "General Billing"}, name="general_billing"),
    path("registration-desk/billing/ip/", views.billing_console_placeholder, {"console_name": "ip", "title": "IP Billing"}, name="ip_billing"),
    path("registration-desk/patient/<int:patient_id>/print/", views.patient_print_card, name="patient_print_card"),
    path("reception-dashboard/", views.reception_dashboard, name="reception_dashboard"),
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-settings/", views.admin_settings, name="admin_settings"),
    path("admin-settings/backup-import/", views.admin_backup_import, name="admin_backup_import"),
    path("admin-settings/export/", views.admin_backup_export, name="admin_backup_export"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("history/", views.history, name="history"),
]
