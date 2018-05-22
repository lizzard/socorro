from django.contrib import admin

from crashstats.tokens.models import Token


@admin.register(Token)
class TokenAdmin(admin.ModelAdmin):
    list_display = [
        'key_truncated',
        'get_user_email',
        'get_permissions',
        'expires',
        'notes',
    ]

    def key_truncated(self, obj):
        return obj.key[:12] + '...'

    def get_permissions(self, obj):
        return ', '.join(perm.codename for perm in obj.permissions.all())

    def get_user_email(self, obj):
        return obj.user.email
