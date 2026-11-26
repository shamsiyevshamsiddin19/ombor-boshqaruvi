"""HTML views: the worker screens (Oldim / Qoldiq qo'shish) and the admin dashboard, Kirim and reports."""

import csv
import io
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.inventory import services
from apps.inventory.models import (
    BaseMaterial,
    InventoryTransaction,
    LeftoverOffcut,
    MaterialCategory,
    MeasurementUnit,
    TransactionType,
)
from apps.users.models import UserRole

from .factories import frozen_at, make_admin, make_material, make_user, stock_up

ADMIN_PAGES = [
    ("inventory:dashboard", []),
    ("inventory:material_list", []),
    ("inventory:material_create", []),
    ("inventory:kirim", []),
    ("inventory:reports", []),
    ("inventory:reports_export", []),
    ("users:pending_users", []),
    ("users:user_list", []),
]


class WorkshopTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.arrachi = make_user("arrachi", [UserRole.ARRACHI], full_name="Bobur Karimov")
        cls.kraskachi = make_user("kraskachi", [UserRole.KRASKACHI], full_name="Dilshod Rahimov")
        cls.mdf = make_material("MDF 16 mm oq", minimum=5)
        cls.paint = make_material(
            "Akril kraska oq", MaterialCategory.LIQUID, MeasurementUnit.LITER, container_size=Decimal("12"), minimum=24
        )
        stock_up(cls.mdf, cls.admin, 10, unit_price="38.50")
        stock_up(cls.paint, cls.admin, 12, unit_price="4.35")  # below its minimum of 24
        cls.offcut = services.register_offcut(material=cls.mdf, user=cls.arrachi, length_mm=1200, width_mm=600, quantity=2)


class AccessTests(WorkshopTestCase):
    def test_anonymous_users_are_sent_to_login(self):
        for name in ("inventory:home", "inventory:offcut_add", "inventory:my_history", "inventory:dashboard"):
            url = reverse(name)
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), f"{reverse('users:login')}?next={url}")

    def test_workers_get_403_on_admin_pages(self):
        self.client.force_login(self.arrachi)
        for name, args in ADMIN_PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)

    def test_admin_pages_render(self):
        self.client.force_login(self.admin)
        for name, args in ADMIN_PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        self.assertEqual(self.client.get(reverse("inventory:material_edit", args=[self.mdf.pk])).status_code, 200)

    def test_worker_cannot_open_another_trades_material_or_offcut(self):
        self.client.force_login(self.kraskachi)
        stock_url = reverse("inventory:withdraw_whole", args=[self.mdf.whole_stock.pk])
        offcut_url = reverse("inventory:offcut_withdraw", args=[self.offcut.pk])
        self.assertEqual(self.client.get(stock_url).status_code, 404)
        self.assertEqual(self.client.post(stock_url, {"quantity": "1"}).status_code, 404)
        self.assertEqual(self.client.post(offcut_url, {"quantity": "1"}).status_code, 404)
        self.mdf.whole_stock.refresh_from_db()
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("10"))


class HomeTests(WorkshopTestCase):
    def test_worker_sees_only_their_trade_in_both_sections(self):
        self.client.force_login(self.kraskachi)
        response = self.client.get(reverse("inventory:home"))
        self.assertContains(response, "Akril kraska oq")
        self.assertNotContains(response, "MDF 16 mm oq")
        self.assertEqual(list(response.context["offcuts"]), [])

        self.client.force_login(self.arrachi)
        response = self.client.get(reverse("inventory:home"))
        self.assertContains(response, "MDF 16 mm oq")
        self.assertNotContains(response, "Akril kraska oq")
        self.assertEqual(list(response.context["offcuts"]), [self.offcut])

    def test_prices_and_valuation_are_admin_only(self):
        self.client.force_login(self.arrachi)
        response = self.client.get(reverse("inventory:home"))
        self.assertNotContains(response, "$385.00")
        self.assertNotIn("nav_valuation_usd", response.context)

        self.client.force_login(self.admin)
        response = self.client.get(reverse("inventory:home"))
        self.assertContains(response, "$385.00")  # MDF line value
        self.assertContains(response, "$437.20")  # header valuation: 385.00 + 52.20

    def test_search_and_category_filter(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("inventory:home"), {"q": "akril"})
        self.assertEqual([s.material.name for s in response.context["whole_stock"]], ["Akril kraska oq"])
        response = self.client.get(reverse("inventory:home"), {"category": MaterialCategory.SHEET})
        self.assertEqual([s.material.name for s in response.context["whole_stock"]], ["MDF 16 mm oq"])


class OldimTests(WorkshopTestCase):
    def setUp(self):
        self.client.force_login(self.arrachi)
        self.url = reverse("inventory:withdraw_whole", args=[self.mdf.whole_stock.pk])

    def test_taking_sheets(self):
        response = self.client.post(self.url, {"quantity": "3", "notes": "Oshxona"}, follow=True)
        self.assertRedirects(response, reverse("inventory:home") + "?tab=whole")
        self.assertContains(response, "Omborda qoldi: 7 dona")
        self.mdf.whole_stock.refresh_from_db()
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("7"))
        entry = InventoryTransaction.objects.chiqim().get()
        self.assertEqual((entry.user, entry.quantity_changed, entry.notes), (self.arrachi, Decimal("-3"), "Oshxona"))

    def test_taking_more_than_the_shelf_shows_an_error(self):
        response = self.client.post(self.url, {"quantity": "11"})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "quantity", "Omborda faqat 10 dona bor.")
        self.assertFalse(InventoryTransaction.objects.chiqim().exists())

    def test_half_a_sheet_is_refused(self):
        response = self.client.post(self.url, {"quantity": "0.5"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)


class QoldiqTests(WorkshopTestCase):
    def setUp(self):
        self.client.force_login(self.arrachi)

    def test_logging_an_offcut(self):
        response = self.client.post(
            reverse("inventory:offcut_add"),
            {"material": self.mdf.pk, "length_mm": "800", "width_mm": "450", "quantity": "1", "notes": "1-stellaj"},
        )
        self.assertRedirects(response, reverse("inventory:home") + "?tab=offcuts")
        self.assertTrue(LeftoverOffcut.objects.filter(length_mm=800, width_mm=450, created_by=self.arrachi).exists())
        self.assertEqual(InventoryTransaction.objects.filter(transaction_type=TransactionType.OFFCUT_DEPOSIT).count(), 2)

    def test_material_list_holds_only_the_workers_offcut_materials(self):
        response = self.client.get(reverse("inventory:offcut_add"))
        self.assertEqual(list(response.context["form"].fields["material"].queryset), [self.mdf])
        self.assertEqual(response.context["sheet_sizes"], {str(self.mdf.pk): [2750, 1830]})

    def test_another_trades_material_is_refused(self):
        self.client.force_login(self.kraskachi)
        response = self.client.post(
            reverse("inventory:offcut_add"), {"material": self.mdf.pk, "length_mm": "800", "width_mm": "450", "quantity": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("material", response.context["form"].errors)
        self.assertEqual(LeftoverOffcut.objects.count(), 1)

    def test_an_offcut_larger_than_the_sheet_is_refused(self):
        response = self.client.post(
            reverse("inventory:offcut_add"), {"material": self.mdf.pk, "length_mm": "3000", "width_mm": "450", "quantity": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "katta bo&#x27;lishi mumkin emas")
        self.assertEqual(LeftoverOffcut.objects.count(), 1)

    def test_taking_offcut_pieces_until_none_are_left(self):
        url = reverse("inventory:offcut_withdraw", args=[self.offcut.pk])
        self.assertRedirects(self.client.post(url, {"quantity": "1"}), reverse("inventory:home") + "?tab=offcuts")
        self.offcut.refresh_from_db()
        self.assertEqual(self.offcut.quantity, 1)

        response = self.client.post(url, {"quantity": "2"})  # only one left
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

        self.client.post(url, {"quantity": "1"})
        self.assertFalse(LeftoverOffcut.objects.filter(pk=self.offcut.pk).exists())
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_my_history_lists_only_my_entries_without_prices(self):
        services.withdraw_whole_material(material=self.paint, user=self.kraskachi, quantity=1)
        services.withdraw_whole_material(material=self.mdf, user=self.arrachi, quantity=2)
        response = self.client.get(reverse("inventory:my_history"))
        self.assertEqual({t.user_id for t in response.context["transactions"]}, {self.arrachi.pk})
        self.assertEqual(len(response.context["transactions"]), 2)  # offcut deposit + sheets
        self.assertNotContains(response, "$77.00")


class KirimViewTests(WorkshopTestCase):
    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("inventory:kirim")

    def test_total_batch_price(self):
        response = self.client.post(
            self.url, {"material": self.mdf.pk, "quantity": "4", "price_mode": "total", "price_usd": "180"}
        )
        self.assertRedirects(response, self.url)
        entry = InventoryTransaction.objects.kirim().order_by("-pk").first()
        self.assertEqual((entry.quantity_changed, entry.unit_price_usd, entry.total_price_usd), (Decimal("4"), Decimal("45.0000"), Decimal("180.00")))
        self.mdf.refresh_from_db()
        self.assertEqual(self.mdf.unit_price_usd, Decimal("40.3571"))  # (385 + 180) / 14

    def test_unit_price(self):
        self.client.post(self.url, {"material": self.mdf.pk, "quantity": "2", "price_mode": "unit", "price_usd": "40"})
        entry = InventoryTransaction.objects.kirim().order_by("-pk").first()
        self.assertEqual(entry.total_price_usd, Decimal("80.00"))

    def test_kirim_quantity_with_total_price(self):
        response = self.client.post(
            self.url,
            {
                "material": self.paint.pk,
                "quantity": "144",
                "price_mode": "total",
                "price_usd": "44646",
                "notes": "Jadval ichidagi tugmani bosganda",
            },
        )
        self.assertRedirects(response, self.url)
        entry = InventoryTransaction.objects.kirim().order_by("-pk").first()
        self.assertEqual(entry.material, self.paint)
        self.assertEqual(entry.quantity_changed, Decimal("144"))
        self.assertEqual(entry.total_price_usd, Decimal("44646.00"))
        self.assertEqual(entry.notes, "Jadval ichidagi tugmani bosganda")

    def test_missing_quantity_is_an_error(self):
        response = self.client.post(self.url, {"material": self.mdf.pk, "price_mode": "total", "price_usd": "10"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("quantity", response.context["form"].errors)

    def test_new_material_goes_straight_to_kirim(self):
        response = self.client.post(
            reverse("inventory:material_create"),
            {"name": "Fanera 12 mm", "category": "FANER", "unit_of_measure": MeasurementUnit.PIECE,
             "minimum_threshold": "6", "length_mm": "2440", "width_mm": "1220"},
        )
        material = BaseMaterial.objects.get(name="Fanera 12 mm")
        self.assertRedirects(response, f"{self.url}?material={material.pk}")
        self.assertEqual(material.allowed_roles, [UserRole.ARRACHI, UserRole.ROVERCHI])  # category default
        self.assertEqual((material.whole_stock.length_mm, material.whole_stock.width_mm), (2440, 1220))

    def test_material_form_rejects_negative_minimum_and_container_size(self):
        url = reverse("inventory:material_create")
        page = self.client.get(url)
        html = page.content.decode()
        self.assertRegex(html, r'name="minimum_threshold"[^>]*min="0"')
        self.assertRegex(html, r'name="container_size"[^>]*min="1"')

        response = self.client.post(url, {
            "name": "Lak X", "category": "KRASKA", "unit_of_measure": MeasurementUnit.LITER,
            "minimum_threshold": "-3", "container_size": "-12",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["form"].errors), {"minimum_threshold", "container_size"})
        self.assertFalse(BaseMaterial.objects.filter(name="Lak X").exists())


class DashboardAndReportTests(WorkshopTestCase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_dashboard_shows_valuation_month_flow_and_low_stock(self):
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(response.context["valuation_usd"], Decimal("437.20"))
        self.assertEqual([s.material.name for s in response.context["low_stock"]], ["Akril kraska oq"])
        self.assertEqual(response.context["current_flow"].kirim_usd, Decimal("437.20"))
        self.assertContains(response, "$437.20")
        self.assertContains(response, "Akril kraska oq")

    def test_dashboard_shows_five_recent_transactions_and_links_to_all_periods(self):
        created = [
            services.withdraw_whole_material(material=self.mdf, user=self.arrachi, quantity=1)
            for _ in range(6)
        ]
        response = self.client.get(reverse("inventory:dashboard"))
        self.assertEqual(list(response.context["recent_transactions"]), list(reversed(created[1:])))
        self.assertContains(response, f'href="{reverse("inventory:reports")}?month="')

    def test_report_with_explicit_empty_month_includes_earlier_periods(self):
        with frozen_at(2000, 1, 10):
            old = services.withdraw_whole_material(material=self.mdf, user=self.arrachi, quantity=1)

        current_report = self.client.get(reverse("inventory:reports"))
        self.assertNotIn(old, current_report.context["transactions"])
        all_periods = self.client.get(reverse("inventory:reports"), {"month": ""})
        self.assertIn(old, all_periods.context["transactions"])

    def test_report_filters_by_worker(self):
        services.withdraw_whole_material(material=self.paint, user=self.kraskachi, quantity=2)
        services.withdraw_whole_material(material=self.mdf, user=self.arrachi, quantity=1)
        response = self.client.get(reverse("inventory:reports"), {"user": self.kraskachi.pk})
        self.assertEqual([t.user for t in response.context["transactions"]], [self.kraskachi])
        self.assertEqual(response.context["totals"]["chiqim_usd"], Decimal("8.70"))

    def test_report_filters_by_month(self):
        with frozen_at(2025, 3, 10):
            services.withdraw_whole_material(material=self.mdf, user=self.arrachi, quantity=1, notes="Eski")
        response = self.client.get(reverse("inventory:reports"), {"month": "2025-03"})
        self.assertEqual([t.notes for t in response.context["transactions"]], ["Eski"])
        self.assertEqual([row["material__name"] for row in response.context["by_material"]], ["MDF 16 mm oq"])

    def test_csv_export(self):
        services.withdraw_whole_material(material=self.mdf, user=self.arrachi, quantity=1, notes="=HYPERLINK(\"x\")")
        response = self.client.get(reverse("inventory:reports_export"), {"category": MaterialCategory.SHEET})
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertTrue(body.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(body.lstrip("﻿"))))
        self.assertEqual(rows[0][0], "Sana")
        self.assertEqual(len(rows), 1 + 3)  # header + kirim, offcut deposit, chiqim (sheets only)
        chiqim = next(r for r in rows[1:] if r[4] == "-1.00")
        self.assertEqual(chiqim[-1], "'=HYPERLINK(\"x\")")  # formula neutralised
        self.assertEqual(chiqim[-2], "Bobur Karimov")
