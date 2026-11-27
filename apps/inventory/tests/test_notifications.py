"""The bell menu: stored events are read one by one; live alerts return when their number changes."""

from django.test import TestCase
from django.urls import reverse

from apps.inventory import notifications
from apps.inventory.models import Notification
from apps.users.models import UserRole

from .factories import make_admin, make_material, make_user


def read_url(key):
    return reverse("inventory:notification_read", args=[key])


class NotificationMenuTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.other_admin = make_admin("ikkinchi")
        self.worker = make_user("bobur", [UserRole.ARRACHI])
        self.client.force_login(self.admin)

    def test_notify_admins_reaches_every_admin_but_no_worker(self):
        notifications.notify_admins(kind="material", message="Kraska qo'shildi", url="/x/")
        self.assertEqual(Notification.objects.filter(message="Kraska qo'shildi").count(), 2)
        self.assertFalse(Notification.objects.filter(recipient=self.worker).exists())

    def test_menu_lists_the_notice_with_a_read_button(self):
        notifications.notify_admins(kind="material", message="Kraska qo'shildi")
        page = self.client.get(reverse("inventory:home"))
        self.assertContains(page, "Kraska qo&#x27;shildi")
        self.assertContains(page, 'data-note-key="n')
        self.assertContains(page, 'aria-label="O\'qildi"')

    def test_reading_one_hides_it_only_for_that_admin(self):
        notifications.notify_admins(kind="material", message="Kraska qo'shildi")
        mine = Notification.objects.get(recipient=self.admin)

        response = self.client.post(read_url(f"n{mine.pk}"))
        self.assertEqual(response.json(), {"ok": True})
        self.assertNotContains(self.client.get(reverse("inventory:home")), "Kraska qo&#x27;shildi")
        self.assertTrue(Notification.objects.filter(recipient=self.other_admin, read_at__isnull=True).exists())

    def test_an_admin_cannot_read_someone_elses_notice(self):
        notifications.notify_admins(kind="material", message="Kraska qo'shildi")
        theirs = Notification.objects.get(recipient=self.other_admin)
        self.client.post(read_url(f"n{theirs.pk}"))
        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)

    def test_unknown_key_is_404(self):
        self.assertEqual(self.client.post(read_url("zzz")).status_code, 404)

    def test_read_all_clears_stored_and_live_entries(self):
        make_material("MDF 16 mm", minimum=5)  # no stock, so it is below its minimum
        make_user("kutayotgan", [UserRole.ARRACHI], approved=False)
        notifications.notify_admins(kind="material", message="Kraska qo'shildi")
        self.assertGreaterEqual(len(notifications.feed_for(self.admin, self.client.session)), 3)

        self.assertEqual(self.client.post(reverse("inventory:notifications_read_all")).json(), {"ok": True})
        self.assertEqual(notifications.feed_for(self.admin, self.client.session), [])

    def test_live_alert_can_be_read_and_returns_when_the_count_changes(self):
        make_material("MDF 16 mm", minimum=5)
        keys = [item["key"] for item in notifications.feed_for(self.admin, self.client.session)]
        self.assertIn("low_stock", keys)

        self.client.post(read_url("low_stock"))
        keys = [item["key"] for item in notifications.feed_for(self.admin, self.client.session)]
        self.assertNotIn("low_stock", keys)

        make_material("LDSP 16 mm", minimum=5)  # a second material runs low
        keys = [item["key"] for item in notifications.feed_for(self.admin, self.client.session)]
        self.assertIn("low_stock", keys)

    def test_workers_and_anonymous_cannot_use_the_endpoints(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.post(read_url("low_stock")).status_code, 403)
        self.assertEqual(self.client.post(reverse("inventory:notifications_read_all")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(read_url("low_stock")).status_code, 302)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(read_url("low_stock")).status_code, 405)

    def test_kirim_is_announced_in_the_menu_not_in_the_page(self):
        material = make_material("Kraska oq", category="KRASKA", unit="LITER")
        response = self.client.post(
            reverse("inventory:kirim"),
            {"material": material.pk, "quantity": "50", "price_mode": "unit", "price_usd": "4"},
        )
        self.assertEqual(response.status_code, 302)
        note = Notification.objects.get(recipient=self.admin)
        self.assertIn("Kraska oq", note.message)
        page = self.client.get(response.url)
        self.assertNotContains(page, "flash-messages")
