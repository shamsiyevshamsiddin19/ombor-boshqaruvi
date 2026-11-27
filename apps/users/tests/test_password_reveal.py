"""The eye in the worker list's password column: one "*" per character, and the real
password only for an administrator who may manage that account."""

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.inventory.tests.factories import PASSWORD, make_admin, make_user
from apps.users.models import User, UserRole


def reveal_url(user):
    return reverse("users:user_password_reveal", args=[user.pk])


class PasswordMaskTests(TestCase):
    def test_list_shows_one_star_per_password_character(self):
        make_user("bobur", [UserRole.ARRACHI])
        self.client.force_login(make_admin())

        response = self.client.get(reverse("users:user_list"))
        self.assertContains(response, "*" * len(PASSWORD))
        self.assertNotContains(response, PASSWORD)

    def test_new_password_changes_the_mask_length(self):
        worker = make_user("bobur", [UserRole.ARRACHI])
        worker.set_password("Qisqa-77x")
        worker.save()
        self.assertEqual(worker.password_mask, "*" * len("Qisqa-77x"))

    def test_account_without_a_stored_copy_has_no_mask(self):
        worker = make_user("bobur", [UserRole.ARRACHI])
        User.objects.filter(pk=worker.pk).update(password_encrypted="")
        worker.refresh_from_db()
        self.assertEqual(worker.password_mask, "")
        self.assertIsNone(worker.revealed_password)

    def test_create_user_and_createsuperuser_store_the_copy(self):
        worker = User.objects.create_user("+998901112233", password="Yangi-ishchi-1")
        root = User.objects.create_superuser("+998901112244", password="Root-parol-22")
        self.assertEqual(worker.revealed_password, "Yangi-ishchi-1")
        self.assertEqual(root.revealed_password, "Root-parol-22")
        self.assertEqual(User.objects.create_user("+998901112255").password_encrypted, "")

    def test_unusable_password_clears_the_copy(self):
        worker = make_user("bobur", [UserRole.ARRACHI])
        worker.set_unusable_password()
        self.assertEqual(worker.password_encrypted, "")


class PasswordRevealEndpointTests(TestCase):
    def setUp(self):
        self.worker = make_user("bobur", [UserRole.ARRACHI])

    def test_admin_gets_the_real_password(self):
        self.client.force_login(make_admin())
        response = self.client.post(reveal_url(self.worker))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"password": PASSWORD})
        self.assertIn("no-store", response["Cache-Control"])

    def test_admin_sees_a_password_set_from_the_edit_form(self):
        self.client.force_login(make_admin())
        self.client.post(
            reverse("users:user_edit", args=[self.worker.pk]),
            {
                "full_name": self.worker.full_name,
                "phone": self.worker.phone,
                "roles": [UserRole.ARRACHI],
                "is_approved": "on",
                "is_active": "on",
                "new_password": "Yangi-Parol-2026",
            },
        )
        self.assertEqual(self.client.post(reveal_url(self.worker)).json(), {"password": "Yangi-Parol-2026"})

    def test_get_is_not_allowed(self):
        self.client.force_login(make_admin())
        self.assertEqual(self.client.get(reveal_url(self.worker)).status_code, 405)

    def test_worker_cannot_reveal(self):
        self.client.force_login(make_user("aziz", [UserRole.ARRACHI]))
        self.assertEqual(self.client.post(reveal_url(self.worker)).status_code, 403)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.post(reveal_url(self.worker))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("users:login"), response["Location"])

    def test_admin_cannot_reveal_another_admin_or_a_superuser(self):
        other_admin = make_admin("ikkinchi")
        root = make_user("root", is_superuser=True, is_staff=True)
        self.client.force_login(make_admin())
        self.assertEqual(self.client.post(reveal_url(other_admin)).status_code, 403)
        self.assertEqual(self.client.post(reveal_url(root)).status_code, 403)

    def test_superuser_can_reveal_an_admin(self):
        admin = make_admin()
        self.client.force_login(make_user("root", is_superuser=True, is_staff=True))
        self.assertEqual(self.client.post(reveal_url(admin)).json(), {"password": PASSWORD})

    def test_missing_copy_is_a_404_with_a_message(self):
        User.objects.filter(pk=self.worker.pk).update(password_encrypted="")
        self.client.force_login(make_admin())
        response = self.client.post(reveal_url(self.worker))
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json())

    def test_rotated_key_makes_the_copy_unreadable(self):
        self.client.force_login(make_admin())
        with override_settings(PASSWORD_REVEAL_KEY="boshqa-kalit"):
            self.assertEqual(self.client.post(reveal_url(self.worker)).status_code, 404)

    def test_reveal_requires_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(make_admin())
        self.assertEqual(client.post(reveal_url(self.worker)).status_code, 403)


class LoginBackfillTests(TestCase):
    """Accounts made before the encrypted copy existed become revealable on their next login."""

    def test_login_stores_the_copy_when_missing(self):
        worker = make_user("bobur", [UserRole.ARRACHI])
        User.objects.filter(pk=worker.pk).update(password_encrypted="")

        response = self.client.post(reverse("users:login"), {"username": worker.phone, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        worker.refresh_from_db()
        self.assertEqual(worker.revealed_password, PASSWORD)

    def test_wrong_password_stores_nothing(self):
        worker = make_user("bobur", [UserRole.ARRACHI])
        User.objects.filter(pk=worker.pk).update(password_encrypted="")

        self.client.post(reverse("users:login"), {"username": worker.phone, "password": "notogri-parol"})
        worker.refresh_from_db()
        self.assertEqual(worker.password_encrypted, "")
