from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FormTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=160)),
                ("page_size", models.CharField(choices=[("A4", "A4"), ("LETTER", "Letter")], default="A4", max_length=12)),
                ("margin_top", models.PositiveIntegerField(default=72)),
                ("margin_right", models.PositiveIntegerField(default=48)),
                ("margin_bottom", models.PositiveIntegerField(default=56)),
                ("margin_left", models.PositiveIntegerField(default=48)),
                ("default_output_mode", models.CharField(choices=[("FLATTENED", "Flattened"), ("FILLABLE", "Fillable")], default="FLATTENED", max_length=12)),
                ("is_deleted", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="form_templates", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-updated_at", "-created_at"],
            },
        ),
        migrations.CreateModel(
            name="FormExportJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("output_mode", models.CharField(choices=[("FLATTENED", "Flattened"), ("FILLABLE", "Fillable")], max_length=12)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("RUNNING", "Running"), ("DONE", "Done"), ("FAILED", "Failed")], default="PENDING", max_length=12)),
                ("output_file", models.FileField(blank=True, null=True, upload_to="form_creator/exports/")),
                ("error_message", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="form_export_jobs", to=settings.AUTH_USER_MODEL)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="export_jobs", to="form_creator.formtemplate")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="FormField",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("type", models.CharField(choices=[("text", "Text"), ("multiline", "Multiline text"), ("number", "Number"), ("date", "Date"), ("dropdown", "Dropdown"), ("checkbox", "Checkbox"), ("radio", "Radio group"), ("signature", "Signature"), ("section", "Section header")], max_length=20)),
                ("label", models.CharField(max_length=160)),
                ("key", models.CharField(max_length=160)),
                ("required", models.BooleanField(default=False)),
                ("x", models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])),
                ("y", models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])),
                ("w", models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])),
                ("h", models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])),
                ("order", models.PositiveIntegerField(default=0)),
                ("options_json", models.JSONField(blank=True, default=dict)),
                ("validation_json", models.JSONField(blank=True, default=dict)),
                ("default_value", models.TextField(blank=True)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fields", to="form_creator.formtemplate")),
            ],
            options={
                "ordering": ["order", "id"],
            },
        ),
        migrations.AddConstraint(
            model_name="formfield",
            constraint=models.UniqueConstraint(fields=("template", "key"), name="unique_form_field_key"),
        ),
    ]
