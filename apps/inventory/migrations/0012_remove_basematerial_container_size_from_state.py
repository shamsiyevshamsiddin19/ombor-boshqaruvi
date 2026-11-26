from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("inventory", "0011_project_model")]

    # Leave the column in place until the version that no longer reads it is live.
    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="basematerial", name="container_size"),
            ],
        ),
    ]
