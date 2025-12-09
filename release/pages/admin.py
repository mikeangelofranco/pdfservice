from django.contrib import admin

from .models import Purchase, ServiceUsage, UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "date_of_birth", "created_at")
    search_fields = ("user__username", "user__email")


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "amount", "credits", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("user__username", "user__email", "description")


@admin.register(ServiceUsage)
class ServiceUsageAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "tool_slug", "created_at")
    search_fields = ("user__username", "user__email", "tool_slug")
