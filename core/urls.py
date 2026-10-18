from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("registration-desk/", views.registration_desk, name="registration_desk"),
    path("registration-desk/patient/<int:patient_id>/print/", views.patient_print_card, name="patient_print_card"),
    path("reception-dashboard/", views.reception_dashboard, name="reception_dashboard"),
    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("admin-settings/", views.admin_settings, name="admin_settings"),
    path("admin-settings/backup-import/", views.admin_backup_import, name="admin_backup_import"),
    path("admin-settings/export/", views.admin_backup_export, name="admin_backup_export"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("history/", views.history, name="history"),
]
