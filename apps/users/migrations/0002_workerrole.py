from django.db import migrations, models

# Same codes and labels as the original fixed UserRole list, so existing data keeps working.
TRADES = [
    ("ARRACHI", "Arrachi", 10),
    ("KRASKACHI", "Kraskachi", 20),
    ("SHKURKACHI", "Shkurkachi", 30),
    ("ROVERCHI", "Roverchi", 40),
    ("SBORKACHI", "Sborkachi", 50),
]


def seed_trades(apps, schema_editor):
    WorkerRole = apps.get_model("users", "WorkerRole")
    for code, name, order in TRADES:
        WorkerRole.objects.get_or_create(code=code, defaults={"name": name, "sort_order": order})


class Migration(migrations.Migration):
    dependencies = [("users", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="WorkerRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=40, unique=True, verbose_name="Kod")),
                ("name", models.CharField(max_length=100, verbose_name="Nomi")),
                ("sort_order", models.PositiveIntegerField(default=100, verbose_name="Tartib")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name": "kasb", "verbose_name_plural": "kasblar", "ordering": ["sort_order", "name"]},
        ),
        migrations.RunPython(seed_trades, lambda apps, se: apps.get_model("users", "WorkerRole").objects.filter(code__in=[c for c, *_ in TRADES]).delete()),
    ]
