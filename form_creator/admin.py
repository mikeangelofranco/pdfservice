from django.contrib import admin

from .models import FormExportJob, FormField, FormTemplate


@admin.register(FormTemplate)
class FormTemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner", "page_size", "default_output_mode", "updated_at")
    list_filter = ("page_size", "default_output_mode", "is_deleted")
    search_fields = ("name", "owner__username", "owner__email")


@admin.register(FormField)
class FormFieldAdmin(admin.ModelAdmin):
    list_display = ("id", "template", "label", "type", "key", "order")
    list_filter = ("type",)
    search_fields = ("label", "key", "template__name")


@admin.register(FormExportJob)
class FormExportJobAdmin(admin.ModelAdmin):
    list_display = ("id", "template", "requested_by", "output_mode", "status", "created_at")
    list_filter = ("status", "output_mode")
    search_fields = ("template__name", "requested_by__username")
