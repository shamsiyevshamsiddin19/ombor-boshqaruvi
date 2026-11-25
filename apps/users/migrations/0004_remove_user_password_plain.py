from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0003_user_password_plain_user_phone"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="user",
            name="password_plain",
        ),
    ]
