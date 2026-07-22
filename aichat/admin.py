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
                    "fallback_models",
                    "api_key",
                    "personality",
                    "max_history",
                    "allowed_channel_ids",
                    "requests_per_minute",
                )
            },
        ),
        (
            "Data exposure (off by default — avoid leaking rare/unreleased collectibles)",
            {"fields": ("allow_stats_lookup", "allow_artwork", "allow_special_events", "allow_web_search")},
        ),
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
