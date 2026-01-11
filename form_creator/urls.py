from django.urls import path

from . import views


urlpatterns = [
    path("tools/form-creator/", views.template_list, name="form_creator"),
    path("tools/form-creator/templates/new/", views.template_new, name="form_creator_new"),
    path(
        "tools/form-creator/templates/<int:template_id>/edit/",
        views.template_edit,
        name="form_creator_edit",
    ),
    path(
        "tools/form-creator/templates/<int:template_id>/save/",
        views.template_save,
        name="form_creator_save",
    ),
    path(
        "tools/form-creator/templates/<int:template_id>/export/",
        views.template_export,
        name="form_creator_export",
    ),
    path(
        "tools/form-creator/templates/<int:template_id>/duplicate/",
        views.template_duplicate,
        name="form_creator_duplicate",
    ),
    path(
        "tools/form-creator/templates/<int:template_id>/delete/",
        views.template_delete,
        name="form_creator_delete",
    ),
    path(
        "api/form-creator/templates/<int:template_id>/",
        views.template_api,
        name="form_creator_api",
    ),
    path(
        "api/form-creator/templates/<int:template_id>/export/",
        views.template_export_api,
        name="form_creator_export_api",
    ),
]
