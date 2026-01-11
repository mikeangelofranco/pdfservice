from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class FormTemplate(models.Model):
    PAGE_SIZES = [
        ("A4", "A4"),
        ("LETTER", "Letter"),
    ]
    OUTPUT_MODES = [
        ("FLATTENED", "Flattened"),
        ("FILLABLE", "Fillable"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="form_templates",
    )
    name = models.CharField(max_length=160)
    page_size = models.CharField(max_length=12, choices=PAGE_SIZES, default="A4")
    margin_top = models.PositiveIntegerField(default=72)
    margin_right = models.PositiveIntegerField(default=48)
    margin_bottom = models.PositiveIntegerField(default=56)
    margin_left = models.PositiveIntegerField(default=48)
    default_output_mode = models.CharField(
        max_length=12,
        choices=OUTPUT_MODES,
        default="FLATTENED",
    )
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.owner})"


class FormField(models.Model):
    FIELD_TYPES = [
        ("text", "Text"),
        ("multiline", "Multiline text"),
        ("number", "Number"),
        ("date", "Date"),
        ("dropdown", "Dropdown"),
        ("checkbox", "Checkbox"),
        ("radio", "Radio group"),
        ("signature", "Signature"),
        ("heading", "Heading"),
        ("paragraph", "Paragraph"),
        ("section", "Section header"),
        ("table", "Table"),
    ]

    template = models.ForeignKey(
        FormTemplate,
        on_delete=models.CASCADE,
        related_name="fields",
    )
    type = models.CharField(max_length=20, choices=FIELD_TYPES)
    label = models.TextField()
    key = models.CharField(max_length=160)
    required = models.BooleanField(default=False)
    x = models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    y = models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    w = models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    h = models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    order = models.PositiveIntegerField(default=0)
    options_json = models.JSONField(default=dict, blank=True)
    validation_json = models.JSONField(default=dict, blank=True)
    default_value = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "key"], name="unique_form_field_key"
            )
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.template})"


class FormExportJob(models.Model):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("RUNNING", "Running"),
        ("DONE", "Done"),
        ("FAILED", "Failed"),
    ]

    template = models.ForeignKey(
        FormTemplate,
        on_delete=models.CASCADE,
        related_name="export_jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="form_export_jobs",
    )
    output_mode = models.CharField(max_length=12, choices=FormTemplate.OUTPUT_MODES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="PENDING")
    output_file = models.FileField(
        upload_to="form_creator/exports/",
        blank=True,
        null=True,
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Export {self.id} ({self.template})"
