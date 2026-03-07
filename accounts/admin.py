# accounts/admin.py

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display   = ['mobile_number', 'username', 'is_superuser', 'is_active', 'date_joined']
    search_fields  = ['mobile_number', 'username']
    ordering       = ['date_joined']
    fieldsets      = (
        (None,          {'fields': ('mobile_number', 'password')}),
        ('Profile',     {'fields': ('username', 'address', 'additional_contacts')}),
        ('Permissions', {'fields': ('is_active', 'is_superuser')}),
        ('Dates',       {'fields': ('date_joined', 'last_login')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields':  ('mobile_number', 'password1', 'password2'),
        }),
    )
    readonly_fields = ['date_joined', 'last_login']
