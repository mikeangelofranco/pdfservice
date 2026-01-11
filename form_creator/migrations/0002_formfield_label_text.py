from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("form_creator", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="formfield",
            name="label",
            field=models.TextField(),
        ),
    ]
