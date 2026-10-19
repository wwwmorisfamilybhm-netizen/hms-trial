from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import UserProfile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name = "Designation Allocation"
    verbose_name_plural = "Designation Allocation"
    fields = ("designation", "notes")
    extra = 0


# Re-register default UserAdmin with inline designation allocation
try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "get_designation",
        "is_staff",
        "is_superuser",
    )
    list_filter = ("profile__designation", "is_staff", "is_superuser", "is_active")

    @admin.display(description="Allocated Designation")
    def get_designation(self, obj):
        if hasattr(obj, "profile"):
            return obj.profile.get_designation_display()
        return "Not Assigned"


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """Dedicated menu in Django Admin to allocate designations to each username."""
    list_display = ("user", "get_full_name", "designation", "notes")
    list_editable = ("designation",)
    list_filter = ("designation",)
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
        "notes",
    )
    autocomplete_fields = ("user",)
    ordering = ("user__username",)

    @admin.display(description="Full Name")
    def get_full_name(self, obj):
        return obj.user.get_full_name() or "—"
