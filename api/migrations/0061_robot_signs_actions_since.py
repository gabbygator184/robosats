from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0060_lnpayment_rewarded_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="robot",
            name="signs_actions_since",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
    ]
