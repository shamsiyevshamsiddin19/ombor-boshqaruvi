import datetime as dt
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.inventory import selectors, services
from apps.inventory.models import InventoryTransaction, LeftoverOffcut, MeasurementUnit
from apps.users.models import UserRole

from .factories import frozen_at, make_admin, make_material, make_user, stock_up


class AuditWorkflowTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.worker = make_user("worker", [UserRole.ARRACHI])
        self.paint = make_material("Audit paint", "KRASKA", MeasurementUnit.LITER)
        self.sheet = make_material("Audit sheet", "LMDF", MeasurementUnit.PIECE)
        self.client.force_login(self.admin)

    def test_kirim_retry_and_new_submission(self):
        url = reverse("inventory:kirim")
        key = self.client.get(url).context["form"].initial["idempotency_key"]
        payload = {"material": self.paint.pk, "quantity": "10", "price_usd": "100", "idempotency_key": key}
        for _ in range(2):
            self.assertRedirects(self.client.post(url, payload), url)
        self.assertEqual(InventoryTransaction.objects.kirim().count(), 1)
        changed = self.client.post(url, dict(payload, price_usd="90"))
        self.assertTrue(changed.context["form"].non_field_errors())
        self.assertRedirects(self.client.post(url, dict(payload, idempotency_key=uuid4())), url)
        self.paint.whole_stock.refresh_from_db()
        self.assertEqual(self.paint.whole_stock.quantity, Decimal("20"))

    def test_material_update_preserves_stock_unit_and_prevents_archive(self):
        stock_up(self.paint, self.admin, 10, unit_price="4")
        edit = reverse("inventory:material_edit", args=[self.paint.pk])
        payload = {
            "name": self.paint.name, "category": "KRASKA", "unit_of_measure": "KG",
            "minimum_threshold": "3", "is_active": "on",
        }
        response = self.client.post(edit, payload)
        self.assertIn("unit_of_measure", response.context["form"].errors)
        payload["unit_of_measure"] = "LITER"
        response = self.client.post(edit, dict(payload, is_active=""))
        self.assertIn("is_active", response.context["form"].errors)
        self.assertRedirects(self.client.post(edit, dict(payload, length_mm=100, width_mm=200)), reverse("inventory:material_list"))
        self.paint.refresh_from_db()
        self.assertEqual(self.paint.minimum_threshold, Decimal("3"))
        self.assertIsNone(self.paint.whole_stock.length_mm)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ru"
        self.assertContains(self.client.get(edit), 'value="3"', count=2)
        response = self.client.patch(
            f"/api/materials/{self.paint.pk}/", {"unit_of_measure": "KG"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_kirim_history_uses_original_unit_and_selected_category(self):
        stock_up(self.paint, self.admin, 10, unit_price="4")
        stock_up(self.sheet, self.admin, 3, unit_price="5")
        # Represents a material whose catalogue unit was changed before the safeguard.
        self.paint.unit_of_measure = MeasurementUnit.KILOGRAM
        self.paint.save(update_fields=["unit_of_measure"])
        response = self.client.get(reverse("inventory:kirim"), {"category": "KRASKA"})
        self.assertContains(response, "+10 L")
        self.assertNotContains(response, "Dona narxi")
        self.assertEqual([row.material_id for row in response.context["recent_kirim"]], [self.paint.pk])

    def test_reversing_receipt_restores_cost_and_keeps_original_entry(self):
        stock_up(self.sheet, self.admin, 10, unit_price="4")
        entry = stock_up(self.sheet, self.admin, 10, unit_price="6")
        reverse_url = reverse("inventory:reverse_transaction", args=[entry.pk])
        self.assertEqual(self.client.get(reverse_url).status_code, 200)
        self.assertRedirects(self.client.post(reverse_url), reverse("inventory:my_history"))
        self.assertRedirects(self.client.post(reverse_url), reverse("inventory:my_history"))
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.unit_price_usd, Decimal("4"))
        self.assertEqual(self.sheet.whole_stock.quantity, Decimal("10"))
        self.assertEqual(InventoryTransaction.objects.filter(reversed_entry=entry).count(), 1)
        self.assertEqual(selectors.transaction_totals(InventoryTransaction.objects.all())["kirim_usd"], Decimal("40"))

    def test_reversal_in_later_month_remains_attributed_to_original_worker(self):
        stock_up(self.sheet, self.admin, 5, unit_price="4")
        with frozen_at(2026, 10, 28):
            withdrawal = services.withdraw_whole_material(material=self.sheet, user=self.worker, quantity=2)
        with frozen_at(2026, 11, 1):
            services.reverse_transaction(entry=withdrawal, user=self.admin)
        qs = selectors.filter_transactions(
            month=dt.date(2026, 11, 1), user_id=self.worker.pk, transaction_type="CHIQIM", include_corrections=True,
        )
        self.assertEqual(selectors.transaction_totals(qs)["chiqim_usd"], Decimal("-8"))
        self.assertEqual(selectors.consumption_by_worker(qs).get()["user__username"], self.worker.username)
        self.assertEqual(selectors.consumption_by_material(qs).get()["quantity"], Decimal("2"))

    def test_offcut_withdrawal_can_be_restored_then_deposit_reversed(self):
        offcut = services.register_offcut(material=self.sheet, user=self.worker, length_mm=400, width_mm=200, quantity=2)
        deposit = InventoryTransaction.objects.get(offcut_record_id=offcut.pk)
        withdrawal = services.withdraw_offcut(offcut=offcut, user=self.worker, quantity=2)
        with self.assertRaises(ValidationError):
            services.reverse_transaction(entry=deposit, user=self.admin)
        services.reverse_transaction(entry=withdrawal, user=self.admin)
        self.assertEqual(LeftoverOffcut.objects.get(pk=offcut.pk).quantity, 2)
        services.reverse_transaction(entry=deposit, user=self.admin)
        self.assertFalse(LeftoverOffcut.objects.filter(pk=offcut.pk).exists())
