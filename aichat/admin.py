from django.contrib import admin

from .models import AIChatSettings, ChatMessage


@admin.register(AIChatSettings)
class AIChatSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "enabled",
                    "model",
                    "api_key",
                    "personality",
                    "max_history",
                    "allowed_channel_ids",
                    "requests_per_minute",
                )
            },
        ),
        ("Music generation (optional, bring your own API)", {"fields": ("music_api_url", "music_api_key")}),
    )

    def has_add_permission(self, request):
        # singleton: only allow adding if no instance exists yet
        return not AIChatSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("channel_id", "author_id", "role", "content", "created_at")
    list_filter = ("role",)
    search_fields = ("channel_id", "author_id", "content")
    ordering = ("-created_at",)
