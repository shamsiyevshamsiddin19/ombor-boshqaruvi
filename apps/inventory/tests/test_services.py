"""3. Kirim, dual inventory deduction (whole vs offcut) and offcut logging; the append-only ledger."""

from decimal import Decimal
from unittest import skipUnless

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from apps.inventory import services
from apps.inventory.models import (
    ImmutableLedgerError,
    InventoryTransaction,
    LeftoverOffcut,
    MaterialCategory,
    MeasurementUnit,
    TransactionType,
)
from apps.inventory.services import InsufficientStockError
from apps.users.models import UserRole

from .factories import make_admin, make_material, make_user, stock_up


class KirimTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.mdf = make_material()

    def test_unit_price_mode(self):
        entry = stock_up(self.mdf, self.admin, 10, unit_price="38.50")
        self.mdf.whole_stock.refresh_from_db()
        self.mdf.refresh_from_db()

        self.assertEqual(entry.transaction_type, TransactionType.KIRIM)
        self.assertEqual(entry.quantity_changed, Decimal("10"))
        self.assertEqual(entry.unit_price_usd, Decimal("38.5000"))
        self.assertEqual(entry.total_price_usd, Decimal("385.00"))
        self.assertEqual(entry.stock_after, Decimal("10"))
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("10"))
        self.assertEqual(self.mdf.unit_price_usd, Decimal("38.5000"))

    def test_total_batch_price_mode_computes_unit_price(self):
        entry = stock_up(self.mdf, self.admin, 3, total="100.00")
        self.assertEqual(entry.total_price_usd, Decimal("100.00"))
        self.assertEqual(entry.unit_price_usd, Decimal("33.3333"))  # 100 / 3, 4 places

    def test_weighted_average_cost_across_batches(self):
        stock_up(self.mdf, self.admin, 10, unit_price="10")
        stock_up(self.mdf, self.admin, 10, unit_price="20")
        self.mdf.refresh_from_db()
        self.assertEqual(self.mdf.unit_price_usd, Decimal("15.0000"))
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("20"))

    def test_average_restarts_when_shelf_was_empty(self):
        worker = make_user("arrachi", [UserRole.ARRACHI])
        stock_up(self.mdf, self.admin, 2, unit_price="10")
        services.withdraw_whole_material(material=self.mdf, user=worker, quantity=2)
        stock_up(self.mdf, self.admin, 4, unit_price="30")
        self.mdf.refresh_from_db()
        self.assertEqual(self.mdf.unit_price_usd, Decimal("30.0000"))

    def test_exactly_one_price_is_required(self):
        with self.assertRaises(ValidationError):
            services.record_kirim(material=self.mdf, user=self.admin, quantity=1)
        with self.assertRaises(ValidationError):
            services.record_kirim(
                material=self.mdf, user=self.admin, quantity=1,
                unit_price_usd=Decimal("1"), total_price_usd=Decimal("1"),
            )
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_rejects_negative_price_and_non_positive_quantity(self):
        with self.assertRaises(ValidationError):
            stock_up(self.mdf, self.admin, 1, unit_price="-1")
        with self.assertRaises(ValidationError):
            stock_up(self.mdf, self.admin, 0, unit_price="1")

    def test_only_admins_record_kirim(self):
        worker = make_user("arrachi", [UserRole.ARRACHI])
        with self.assertRaises(PermissionDenied):
            stock_up(self.mdf, worker, 5)
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_sheets_move_in_whole_numbers(self):
        with self.assertRaises(ValidationError):
            stock_up(self.mdf, self.admin, "1.5")

    def test_liquids_accept_fractions(self):
        paint = make_material("Lak", MaterialCategory.LIQUID, MeasurementUnit.LITER)
        entry = stock_up(paint, self.admin, "2.5", total="20")
        self.assertEqual(entry.quantity_changed, Decimal("2.50"))
        self.assertEqual(entry.unit_price_usd, Decimal("8.0000"))

    def test_archived_material_cannot_receive_stock(self):
        self.mdf.is_active = False
        self.mdf.save()
        with self.assertRaises(ValidationError):
            stock_up(self.mdf, self.admin, 1)


class WholeWithdrawalTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.worker = make_user("arrachi", [UserRole.ARRACHI])
        self.mdf = make_material()
        stock_up(self.mdf, self.admin, 10, unit_price="38.50")

    def test_withdrawal_reduces_stock_and_writes_ledger(self):
        entry = services.withdraw_whole_material(material=self.mdf, user=self.worker, quantity=3, notes=" Oshxona ")

        self.mdf.whole_stock.refresh_from_db()
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("7"))
        self.assertEqual(entry.transaction_type, TransactionType.CHIQIM)
        self.assertEqual(entry.quantity_changed, Decimal("-3"))
        self.assertEqual(entry.quantity_abs, Decimal("3"))
        self.assertEqual(entry.unit_price_usd, Decimal("38.5000"))
        self.assertEqual(entry.total_price_usd, Decimal("115.50"))
        self.assertEqual(entry.stock_after, Decimal("7"))
        self.assertEqual(entry.user, self.worker)
        self.assertEqual(entry.notes, "Oshxona")
        self.assertFalse(entry.is_offcut)

    def test_withdrawal_keeps_the_average_price(self):
        services.withdraw_whole_material(material=self.mdf, user=self.worker, quantity=4)
        self.mdf.refresh_from_db()
        self.assertEqual(self.mdf.unit_price_usd, Decimal("38.5000"))

    def test_cannot_take_more_than_is_on_the_shelf(self):
        with self.assertRaises(InsufficientStockError) as ctx:
            services.withdraw_whole_material(material=self.mdf, user=self.worker, quantity=11)
        self.assertIn("quantity", ctx.exception.message_dict)
        self.mdf.whole_stock.refresh_from_db()
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("10"))
        self.assertEqual(InventoryTransaction.objects.chiqim().count(), 0)

    def test_can_empty_the_shelf_exactly(self):
        entry = services.withdraw_whole_material(material=self.mdf, user=self.worker, quantity=10)
        self.assertEqual(entry.stock_after, Decimal("0"))

    def test_other_trades_cannot_withdraw(self):
        painter = make_user("kraskachi", [UserRole.KRASKACHI])
        with self.assertRaises(PermissionDenied):
            services.withdraw_whole_material(material=self.mdf, user=painter, quantity=1)

    def test_pending_users_cannot_withdraw(self):
        pending = make_user("pending", [UserRole.ARRACHI], approved=False)
        with self.assertRaises(PermissionDenied):
            services.withdraw_whole_material(material=self.mdf, user=pending, quantity=1)

    def test_whole_number_rule_on_withdrawal(self):
        with self.assertRaises(ValidationError):
            services.withdraw_whole_material(material=self.mdf, user=self.worker, quantity=Decimal("0.5"))

    def test_fractional_litres_are_withdrawn_at_cost(self):
        painter = make_user("kraskachi", [UserRole.KRASKACHI])
        paint = make_material("Akril", MaterialCategory.LIQUID, MeasurementUnit.LITER)
        stock_up(paint, self.admin, 12, unit_price="4.35")
        entry = services.withdraw_whole_material(material=paint, user=painter, quantity=Decimal("2.5"))
        self.assertEqual(entry.total_price_usd, Decimal("10.88"))  # 2.5 x 4.35 = 10.875, half up
        paint.whole_stock.refresh_from_db()
        self.assertEqual(paint.whole_stock.quantity, Decimal("9.50"))


class OffcutTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.worker = make_user("arrachi", [UserRole.ARRACHI])
        self.mdf = make_material(length_mm=2750, width_mm=1830)
        stock_up(self.mdf, self.admin, 10, unit_price="38.50")

    def test_register_offcut_logs_a_zero_value_deposit(self):
        offcut = services.register_offcut(
            material=self.mdf, user=self.worker, length_mm=1200, width_mm=600, quantity=2, notes="2-stellaj"
        )
        self.assertEqual((offcut.length_mm, offcut.width_mm, offcut.quantity), (1200, 600, 2))
        self.assertEqual(offcut.created_by, self.worker)
        self.assertEqual(offcut.area_m2, Decimal("0.720"))

        entry = InventoryTransaction.objects.get(transaction_type=TransactionType.OFFCUT_DEPOSIT)
        self.assertTrue(entry.is_offcut)
        self.assertEqual(entry.quantity_changed, Decimal("2"))
        self.assertEqual((entry.offcut_length_mm, entry.offcut_width_mm), (1200, 600))
        self.assertEqual(entry.total_price_usd, Decimal("0"))
        self.assertIsNone(entry.stock_after)

    def test_dual_inventory_is_kept_apart(self):
        """Taking a whole sheet never touches offcuts, and reusing an offcut never touches whole stock."""
        offcut = services.register_offcut(material=self.mdf, user=self.worker, length_mm=800, width_mm=450, quantity=3)
        self.mdf.whole_stock.refresh_from_db()
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("10"))  # logging an offcut adds no whole stock

        services.withdraw_whole_material(material=self.mdf, user=self.worker, quantity=2)
        offcut.refresh_from_db()
        self.assertEqual(offcut.quantity, 3)

        services.withdraw_offcut(offcut=offcut, user=self.worker, quantity=1)
        self.mdf.whole_stock.refresh_from_db()
        self.assertEqual(self.mdf.whole_stock.quantity, Decimal("8"))
        offcut.refresh_from_db()
        self.assertEqual(offcut.quantity, 2)

    def test_partial_then_full_offcut_withdrawal(self):
        offcut = services.register_offcut(material=self.mdf, user=self.worker, length_mm=950, width_mm=700, quantity=3)

        first = services.withdraw_offcut(offcut=offcut, user=self.worker, quantity=1, notes="Polka")
        offcut.refresh_from_db()
        self.assertEqual(offcut.quantity, 2)
        self.assertEqual(first.transaction_type, TransactionType.CHIQIM)
        self.assertTrue(first.is_offcut)
        self.assertEqual(first.quantity_changed, Decimal("-1"))
        self.assertEqual(first.total_price_usd, Decimal("0"))
        self.assertEqual((first.offcut_length_mm, first.offcut_width_mm), (950, 700))

        last = services.withdraw_offcut(offcut=offcut, user=self.worker, quantity=2)
        self.assertFalse(LeftoverOffcut.objects.filter(pk=offcut.pk).exists())
        # The ledger keeps the size of the piece even though the offcut row is gone.
        self.assertEqual((last.offcut_length_mm, last.offcut_width_mm), (950, 700))
        self.assertEqual(InventoryTransaction.objects.filter(is_offcut=True).count(), 3)

    def test_cannot_take_more_pieces_than_exist(self):
        offcut = services.register_offcut(material=self.mdf, user=self.worker, length_mm=600, width_mm=400, quantity=1)
        with self.assertRaises(InsufficientStockError):
            services.withdraw_offcut(offcut=offcut, user=self.worker, quantity=2)
        with self.assertRaises(ValidationError):
            services.withdraw_offcut(offcut=offcut, user=self.worker, quantity=0)
        offcut.refresh_from_db()
        self.assertEqual(offcut.quantity, 1)

    def test_offcut_taken_by_someone_else_first(self):
        offcut = services.register_offcut(material=self.mdf, user=self.worker, length_mm=600, width_mm=400)
        stale = LeftoverOffcut.objects.get(pk=offcut.pk)
        services.withdraw_offcut(offcut=offcut, user=self.worker)
        with self.assertRaises(InsufficientStockError):
            services.withdraw_offcut(offcut=stale, user=self.worker)

    def test_offcut_cannot_be_bigger_than_the_sheet(self):
        with self.assertRaises(ValidationError):
            services.register_offcut(material=self.mdf, user=self.worker, length_mm=2800, width_mm=500)
        with self.assertRaises(ValidationError):
            services.register_offcut(material=self.mdf, user=self.worker, length_mm=1900, width_mm=1900)
        self.assertFalse(LeftoverOffcut.objects.exists())

    def test_offcut_may_be_entered_rotated(self):
        offcut = services.register_offcut(material=self.mdf, user=self.worker, length_mm=1830, width_mm=2750)
        self.assertEqual(offcut.width_mm, 2750)

    def test_dimensions_and_count_must_be_positive_integers(self):
        for bad in ({"length_mm": 0}, {"width_mm": -5}, {"quantity": 0}, {"length_mm": 12.5}):
            kwargs = {"length_mm": 600, "width_mm": 400, "quantity": 1, **bad}
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                services.register_offcut(material=self.mdf, user=self.worker, **kwargs)

    def test_hardware_and_liquids_take_offcuts_without_a_size(self):
        hinge = make_material("Petlya", MaterialCategory.HARDWARE, MeasurementUnit.PIECE)
        offcut = services.register_offcut(material=hinge, user=self.admin, length_mm=10, width_mm=10, quantity=3)
        self.assertEqual((offcut.length_mm, offcut.width_mm, offcut.quantity), (None, None, 3))
        self.assertIsNone(offcut.area_m2)
        self.assertIsNone(offcut.estimated_value_usd)
        deposit = InventoryTransaction.objects.get(transaction_type=TransactionType.OFFCUT_DEPOSIT, material=hinge)
        self.assertIsNone(deposit.offcut_length_mm)

    def test_sheet_offcut_still_needs_a_size(self):
        with self.assertRaises(ValidationError):
            services.register_offcut(material=self.mdf, user=self.worker, length_mm=None, width_mm=None)

    def test_size_less_offcut_can_be_taken_and_the_take_reversed(self):
        paint = make_material("Lak mat", MaterialCategory.LIQUID, MeasurementUnit.LITER)
        painter = make_user("kraskachi", [UserRole.KRASKACHI])
        offcut = services.register_offcut(material=paint, user=painter, quantity=2, notes="yarim banka")
        entry = services.withdraw_offcut(offcut=offcut, user=painter, quantity=2)
        self.assertFalse(LeftoverOffcut.objects.filter(pk=offcut.pk).exists())
        services.reverse_transaction(entry=entry, user=self.admin, reason="xato")
        restored = LeftoverOffcut.objects.get(pk=offcut.pk)
        self.assertEqual((restored.length_mm, restored.width_mm, restored.quantity), (None, None, 2))

    def test_other_trades_cannot_log_or_take_offcuts(self):
        painter = make_user("kraskachi", [UserRole.KRASKACHI])
        offcut = services.register_offcut(material=self.mdf, user=self.worker, length_mm=600, width_mm=400)
        with self.assertRaises(PermissionDenied):
            services.register_offcut(material=self.mdf, user=painter, length_mm=600, width_mm=400)
        with self.assertRaises(PermissionDenied):
            services.withdraw_offcut(offcut=offcut, user=painter)

    def test_estimated_offcut_value_is_an_area_share_of_the_sheet(self):
        offcut = services.register_offcut(
            material=self.mdf, user=self.worker, length_mm=1375, width_mm=915, quantity=2
        )  # exactly a quarter sheet each
        offcut = LeftoverOffcut.objects.select_related("material__whole_stock").get(pk=offcut.pk)
        self.assertEqual(offcut.estimated_value_usd, Decimal("19.25"))  # 2 x 38.50 / 4


class LedgerImmutabilityTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.mdf = make_material()
        self.entry = stock_up(self.mdf, self.admin, 5)

    def test_saved_entries_cannot_be_edited(self):
        self.entry.notes = "tampered"
        with self.assertRaises(ImmutableLedgerError):
            self.entry.save()

    def test_entries_cannot_be_deleted(self):
        with self.assertRaises(ImmutableLedgerError):
            self.entry.delete()

    def test_bulk_update_and_delete_are_blocked(self):
        with self.assertRaises(ImmutableLedgerError):
            InventoryTransaction.objects.filter(pk=self.entry.pk).update(notes="tampered")
        with self.assertRaises(ImmutableLedgerError):
            InventoryTransaction.objects.all().delete()
        self.assertEqual(InventoryTransaction.objects.count(), 1)

    def test_material_with_history_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.mdf.delete()

    def test_database_rejects_an_impossible_sign(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            InventoryTransaction.objects.create(
                transaction_type=TransactionType.CHIQIM, material=self.mdf, user=self.admin,
                quantity_changed=Decimal("5"), unit_price_usd=Decimal("1"), total_price_usd=Decimal("5"),
            )

    @skipUnless(connection.vendor == "postgresql", "append-only trigger is PostgreSQL-only")
    def test_postgres_trigger_blocks_raw_sql_changes(self):
        table = InventoryTransaction._meta.db_table
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET notes = 'tampered' WHERE id = %s", [self.entry.pk])
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [self.entry.pk])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.notes, "")
