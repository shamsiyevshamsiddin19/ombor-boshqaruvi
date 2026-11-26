from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("inventory", "0012_remove_basematerial_container_size_from_state")]

    # The preceding release removed this field from the application and migration state.
    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE inventory_basematerial DROP COLUMN container_size",
            reverse_sql="ALTER TABLE inventory_basematerial ADD COLUMN container_size numeric(10, 2) NULL",
        ),
    ]
