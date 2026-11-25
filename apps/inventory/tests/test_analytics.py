"""4. Asset valuation, minimum-stock alerts and the monthly Kirim / Chiqim history."""

import datetime as dt
from decimal import Decimal

from django.test import TestCase

from apps.inventory import selectors, services
from apps.inventory.models import MaterialCategory, MeasurementUnit, TransactionType, WholeMaterial
from apps.users.models import UserRole

from .factories import frozen_at, make_admin, make_material, make_user, stock_up


class ValuationTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.worker = make_user("arrachi", [UserRole.ARRACHI])

    def test_empty_warehouse_is_worth_nothing(self):
        self.assertEqual(selectors.warehouse_valuation_usd(), Decimal("0.00"))

    def test_valuation_is_quantity_times_unit_price(self):
        mdf = make_material("MDF")
        paint = make_material("Kraska", MaterialCategory.LIQUID, MeasurementUnit.LITER)
        stock_up(mdf, self.admin, 10, unit_price="38.50")
        stock_up(paint, self.admin, 12, unit_price="4.35")
        self.assertEqual(selectors.warehouse_valuation_usd(), Decimal("437.20"))  # 385.00 + 52.20

        services.withdraw_whole_material(material=mdf, user=self.worker, quantity=2)
        self.assertEqual(selectors.warehouse_valuation_usd(), Decimal("360.20"))  # 308.00 + 52.20

    def test_valuation_uses_the_weighted_average_price(self):
        mdf = make_material("MDF")
        stock_up(mdf, self.admin, 10, unit_price="10")
        stock_up(mdf, self.admin, 10, total="300")  # 10 more at $30 -> average $20
        self.assertEqual(selectors.warehouse_valuation_usd(), Decimal("400.00"))

    def test_each_line_is_rounded_so_the_total_matches_the_table(self):
        for name in ("Samarez A", "Samarez B", "Samarez C"):
            stock_up(make_material(name, MaterialCategory.HARDWARE, MeasurementUnit.PIECE), self.admin, 7, unit_price="0.1234")
        lines = [stock.total_value_usd for stock in WholeMaterial.objects.select_related("material")]
        self.assertEqual(lines, [Decimal("0.86")] * 3)  # 7 x 0.1234 = 0.8638 each
        self.assertEqual(selectors.warehouse_valuation_usd(), sum(lines))  # 2.58, not round(2.5914) = 2.59

    def test_offcuts_are_not_part_of_the_valuation(self):
        mdf = make_material("MDF")
        stock_up(mdf, self.admin, 4, unit_price="40")
        services.register_offcut(material=mdf, user=self.worker, length_mm=1200, width_mm=600, quantity=5)
        self.assertEqual(selectors.warehouse_valuation_usd(), Decimal("160.00"))
        self.assertGreater(selectors.estimated_offcut_value_usd(), 0)


class ThresholdAlertTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.worker = make_user("sborkachi", [UserRole.SBORKACHI])
        self.hinge = make_material("Petlya", MaterialCategory.HARDWARE, MeasurementUnit.PIECE, minimum=10)

    def low(self):
        return [stock.material.name for stock in selectors.low_stock_items()]

    def test_below_minimum_is_flagged_with_its_shortfall(self):
        stock_up(self.hinge, self.admin, 9)
        stock = WholeMaterial.objects.get(material=self.hinge)
        self.assertTrue(stock.is_below_threshold)
        self.assertEqual(stock.shortfall, Decimal("1"))
        self.assertEqual(self.low(), ["Petlya"])

    def test_exactly_at_minimum_is_not_an_alert(self):
        stock_up(self.hinge, self.admin, 10)
        self.assertFalse(WholeMaterial.objects.get(material=self.hinge).is_below_threshold)
        self.assertEqual(self.low(), [])

    def test_a_withdrawal_can_trigger_the_alert_and_a_kirim_clears_it(self):
        stock_up(self.hinge, self.admin, 12)
        self.assertEqual(self.low(), [])
        services.withdraw_whole_material(material=self.hinge, user=self.worker, quantity=5)
        self.assertEqual(self.low(), ["Petlya"])
        stock_up(self.hinge, self.admin, 3)
        self.assertEqual(self.low(), [])

    def test_materials_without_a_minimum_never_alert(self):
        make_material("Tutqich", MaterialCategory.HARDWARE, MeasurementUnit.PIECE, minimum=0)
        self.assertEqual(self.low(), ["Petlya"])  # Petlya: 0 < 10; Tutqich: 0 is not < 0

    def test_archived_materials_do_not_alert(self):
        self.hinge.is_active = False
        self.hinge.save()
        self.assertEqual(self.low(), [])

    def test_alerts_are_ordered_by_category_then_name(self):
        make_material("Akril", MaterialCategory.LIQUID, MeasurementUnit.LITER, minimum=5)
        make_material("Fanera", minimum=3)
        make_material("Boltlar", MaterialCategory.HARDWARE, MeasurementUnit.PIECE, minimum=1)
        self.assertEqual(self.low(), ["Boltlar", "Petlya", "Akril", "Fanera"])  # HARDWARE < LIQUID < SHEET


class MonthlyFlowTests(TestCase):
    """Sept: +$100 / -$20. Oct: +$80 / -$12.31. Nov: -$36.92 plus $0 offcut moves."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.arrachi = make_user("arrachi", [UserRole.ARRACHI])
        cls.roverchi = make_user("roverchi", [UserRole.ROVERCHI])
        cls.mdf = make_material("MDF")
        cls.paint = make_material("Lak", MaterialCategory.LIQUID, MeasurementUnit.LITER)

        with frozen_at(2026, 9, 3):
            stock_up(cls.mdf, cls.admin, 10, unit_price="10")
        with frozen_at(2026, 9, 20):
            services.withdraw_whole_material(material=cls.mdf, user=cls.arrachi, quantity=2)
        with frozen_at(2026, 10, 2):
            stock_up(cls.mdf, cls.admin, 5, unit_price="16")  # average (80 + 80) / 13 = 12.3077
        with frozen_at(2026, 10, 31, hour=23):
            services.withdraw_whole_material(material=cls.mdf, user=cls.roverchi, quantity=1)  # still October locally
        with frozen_at(2026, 11, 1, hour=0):
            services.withdraw_whole_material(material=cls.mdf, user=cls.arrachi, quantity=3)
        with frozen_at(2026, 11, 5):
            offcut = services.register_offcut(material=cls.mdf, user=cls.arrachi, length_mm=900, width_mm=500)
            services.withdraw_offcut(offcut=offcut, user=cls.roverchi)

    def test_recent_month_flows(self):
        flows = selectors.recent_month_flows(4, today=dt.date(2026, 11, 19))
        self.assertEqual(
            [(f.month, f.kirim_usd, f.chiqim_usd, f.kirim_count, f.chiqim_count) for f in flows],
            [
                (dt.date(2026, 8, 1), Decimal("0.00"), Decimal("0.00"), 0, 0),
                (dt.date(2026, 9, 1), Decimal("100.00"), Decimal("20.00"), 1, 1),
                (dt.date(2026, 10, 1), Decimal("80.00"), Decimal("12.31"), 1, 1),
                (dt.date(2026, 11, 1), Decimal("0.00"), Decimal("36.92"), 0, 2),
            ],
        )
        self.assertEqual(flows[1].net_usd, Decimal("80.00"))

    def test_single_month_matches_the_series(self):
        october = selectors.monthly_flow(2026, 10)
        self.assertEqual((october.kirim_usd, october.chiqim_usd), (Decimal("80.00"), Decimal("12.31")))

    def test_month_bounds_roll_over_the_year(self):
        start, end = selectors.month_bounds(2026, 12)
        self.assertEqual((start.date(), end.date()), (dt.date(2026, 12, 1), dt.date(2027, 1, 1)))
        flows = selectors.recent_month_flows(2, today=dt.date(2027, 1, 5))
        self.assertEqual([f.month for f in flows], [dt.date(2026, 12, 1), dt.date(2027, 1, 1)])

    def test_filter_by_month_category_worker_material_and_type(self):
        october = dt.date(2026, 10, 1)
        november = dt.date(2026, 11, 1)
        self.assertEqual(selectors.filter_transactions(month=october).count(), 2)
        self.assertEqual(selectors.filter_transactions(month=november).count(), 3)  # chiqim, deposit, offcut chiqim
        self.assertEqual(selectors.filter_transactions(user_id=self.roverchi.pk).count(), 2)
        self.assertEqual(selectors.filter_transactions(material_id=self.paint.pk).count(), 0)
        self.assertEqual(selectors.filter_transactions(category=MaterialCategory.LIQUID).count(), 0)
        self.assertEqual(selectors.filter_transactions(category=MaterialCategory.SHEET).count(), 7)
        self.assertEqual(
            selectors.filter_transactions(month=november, transaction_type=TransactionType.OFFCUT_DEPOSIT).count(), 1
        )

    def test_report_totals(self):
        totals = selectors.transaction_totals(selectors.filter_transactions(month=dt.date(2026, 11, 1)))
        self.assertEqual(totals["kirim_usd"], Decimal("0.00"))
        self.assertEqual(totals["chiqim_usd"], Decimal("36.92"))
        self.assertEqual(totals["net_usd"], Decimal("-36.92"))
        self.assertEqual(totals["net_abs"], Decimal("36.92"))
        self.assertEqual(totals["offcut_count"], 1)

    def test_consumption_by_worker_and_material(self):
        qs = selectors.filter_transactions()
        by_worker = {row["user__username"]: row["total_usd"] for row in selectors.consumption_by_worker(qs)}
        self.assertEqual(by_worker, {"arrachi": Decimal("56.92"), "roverchi": Decimal("12.31")})

        rows = list(selectors.consumption_by_material(qs))
        whole_rows = [r for r in rows if not r["is_offcut"]]
        offcut_rows = [r for r in rows if r["is_offcut"]]
        self.assertEqual(len(whole_rows), 1)
        self.assertEqual(whole_rows[0]["material__name"], "MDF")
        self.assertEqual(whole_rows[0]["quantity"], Decimal("-6"))  # 2 + 1 + 3 sheets
        self.assertEqual(whole_rows[0]["total_usd"], Decimal("69.23"))
        self.assertEqual(whole_rows[0]["entries"], 3)
        self.assertEqual(len(offcut_rows), 1)
        self.assertEqual(offcut_rows[0]["quantity"], Decimal("-1"))
        self.assertEqual(offcut_rows[0]["total_usd"], Decimal("0.00"))
        self.assertEqual(offcut_rows[0]["entries"], 1)
