from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.users.models import User, UserRole


class SeedDemoCommandTests(TestCase):
    def test_seed_demo_users_only_and_idempotent(self):
        out = StringIO()
        call_command("seed_demo", "--users-only", force=True, stdout=out)
        output = out.getvalue()

        self.assertIn("Demo users loaded: 6 users.", output)
        self.assertIn("+998 90 000 00 01", output)
        self.assertIn("Akmal Rustamov", output)
        self.assertIn("+998 90 000 00 06", output)
        self.assertIn("tasdiq kutmoqda", output)

        admin = User.objects.get(phone="+998900000001")
        self.assertEqual(admin.full_name, "Akmal Rustamov")
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password("ombor-demo-2026"))

        arrachi = User.objects.get(phone="+998900000002")
        self.assertEqual(arrachi.full_name, "Bobur Karimov")
        self.assertEqual(arrachi.roles, [UserRole.ARRACHI.value])
        self.assertTrue(arrachi.is_approved)
        self.assertTrue(arrachi.check_password("ombor-demo-2026"))

        pending = User.objects.get(phone="+998900000006")
        self.assertEqual(pending.full_name, "Otabek Nazarov")
        self.assertFalse(pending.is_approved)

        # Idempotent re-run
        out2 = StringIO()
        call_command("seed_demo", "--users-only", "--password", "new-password-123", force=True, stdout=out2)
        admin.refresh_from_db()
        self.assertTrue(admin.check_password("new-password-123"))

