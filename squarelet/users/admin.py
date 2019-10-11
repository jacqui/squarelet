# Django
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as AuthUserAdmin
from django.contrib.auth.forms import UserCreationForm
from django.utils.translation import ugettext_lazy as _

# Third Party
from allauth.account.utils import setup_user_email, sync_user_email_addresses
from reversion.admin import VersionAdmin

# Squarelet
from squarelet.organizations.models import Organization

# Local
from .models import User


class MyUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")


@admin.register(User)
class MyUserAdmin(VersionAdmin, AuthUserAdmin):
    add_form = MyUserCreationForm
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "username", "password1", "password2"),
            },
        ),
    )
    fieldsets = (
        (None, {"fields": ("username", "password", "can_change_username")}),
        (_("Personal info"), {"fields": ("name", "email")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "created_at", "updated_at")}),
    )
    readonly_fields = ("created_at", "updated_at")
    list_display = ("username", "name", "is_superuser")
    search_fields = ("username", "name", "email")

    def save_model(self, request, obj, form, change):
        """Sync all auth email addresses"""
        if change:
            super().save_model(request, obj, form, change)
            sync_user_email_addresses(obj)
        else:
            Organization.objects.create_individual(obj)
            setup_user_email(request, obj, [])
