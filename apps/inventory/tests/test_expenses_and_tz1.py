"""Tests for TZ 1-qism additions: SQM unit, fractional Kirim, category cascade, and OperatingExpense."""

import csv
import datetime as dt
import io
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.inventory import selectors, services
from apps.inventory.models import ExpenseCategory, MeasurementUnit, OperatingExpense
from apps.users.models import UserRole

from .factories import make_admin, make_material, make_user


class TzPartOneInventoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.arrachi = make_user("arrachi_tz", [UserRole.ARRACHI])

    def test_sqm_unit_and_fractional_kirim_in_form(self):
        shpon = make_material("Shpon emal", category="LMDF", unit=MeasurementUnit.SQM)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("inventory:kirim"),
            {"material": shpon.pk, "quantity": "12.5", "price_mode": "total", "price_usd": "125.00"},
        )
        self.assertRedirects(response, reverse("inventory:kirim"))
        shpon.whole_stock.refresh_from_db()
        shpon.refresh_from_db()
        self.assertEqual(shpon.whole_stock.quantity, Decimal("12.50"))
        self.assertEqual(shpon.unit_price_usd, Decimal("10.0000"))
        self.assertEqual(shpon.unit_short, "m²")

    def test_piece_unit_still_rejects_fractional_kirim_in_form(self):
        lmdf = make_material("LMDF 18 mm", category="LMDF", unit=MeasurementUnit.PIECE)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("inventory:kirim"),
            {"material": lmdf.pk, "quantity": "1.5", "price_mode": "unit", "price_usd": "25.00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("quantity", response.context["form"].errors)

    def test_offcuts_work_for_seeded_lmdf_category(self):
        lmdf = make_material("LMDF 16 mm", category="LMDF", unit=MeasurementUnit.PIECE, length_mm=2750, width_mm=1830)
        self.assertTrue(lmdf.supports_offcuts)
        self.assertIn(lmdf, selectors.offcut_capable_materials(self.arrachi))
        offcut = services.register_offcut(material=lmdf, user=self.arrachi, length_mm=900, width_mm=500, quantity=2)
        self.assertEqual(offcut.quantity, 2)


class OperatingExpenseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.worker = make_user("worker_exp", [UserRole.SBORKACHI])

    def test_expense_service_validation_and_permissions(self):
        with self.assertRaises(PermissionDenied):
            services.record_expense(
                user=self.worker,
                category=ExpenseCategory.STANOK,
                title="Arra pichog'i",
                amount_usd="15.00",
            )
        with self.assertRaises(ValidationError):
            services.record_expense(user=self.admin, category="   ", title="X", amount_usd="10")
        with self.assertRaises(ValidationError):
            services.record_expense(user=self.admin, category=ExpenseCategory.NALOG, title="  ", amount_usd="10")
        with self.assertRaises(ValidationError):
            services.record_expense(user=self.admin, category=ExpenseCategory.NALOG, title="Soliq", amount_usd="0")

    def test_expense_views_filtering_dashboard_and_csv(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get(reverse("inventory:expenses")).status_code, 403)
        self.assertEqual(self.client.get(reverse("inventory:expenses_export")).status_code, 403)

        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("inventory:expenses"),
            {
                "category": ExpenseCategory.STANOK,
                "title": "Arra pichog'i charxlash",
                "amount_usd": "35.50",
                "expense_date": "2026-11-15",
                "notes": "Stanok #2",
            },
        )
        self.assertRedirects(resp, reverse("inventory:expenses"))
        services.record_expense(
            user=self.admin,
            category=ExpenseCategory.PRODUKTA,
            title="Tushlik va choy",
            amount_usd="24.50",
            expense_date=dt.date(2026, 11, 20),
        )
        services.record_expense(
            user=self.admin,
            category=ExpenseCategory.NALOG,
            title="Oylik soliq",
            amount_usd="100.00",
            expense_date=dt.date(2026, 10, 10),
        )

        nov_page = self.client.get(reverse("inventory:expenses"), {"month": "2026-11"})
        self.assertEqual(nov_page.status_code, 200)
        self.assertEqual(nov_page.context["totals"]["total_usd"], Decimal("60.00"))
        self.assertEqual(nov_page.context["totals"]["stanok_usd"], Decimal("35.50"))
        self.assertEqual(nov_page.context["totals"]["produkta_usd"], Decimal("24.50"))

        csv_resp = self.client.get(reverse("inventory:expenses_export"), {"month": "2026-11"})
        self.assertEqual(csv_resp["Content-Type"], "text/csv; charset=utf-8")
        rows = list(csv.reader(io.StringIO(b"".join(csv_resp.streaming_content).decode("utf-8").lstrip("\ufeff"))))
        self.assertEqual(len(rows), 3)

        reports_resp = self.client.get(reverse("inventory:reports"), {"month": "2026-11"})
        self.assertEqual(reports_resp.context["expense_totals"]["total_usd"], Decimal("60.00"))

    def test_expense_api_endpoint(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get("/api/expenses/").status_code, 403)

        self.client.force_login(self.admin)
        res = self.client.post(
            "/api/expenses/",
            {
                "category": ExpenseCategory.NALOG,
                "title": "QQS to'lovi",
                "amount_usd": "120.00",
                "expense_date": "2026-11-25",
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(OperatingExpense.objects.count(), 1)
        listed = self.client.get("/api/expenses/").json()["results"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["title"], "QQS to'lovi")

