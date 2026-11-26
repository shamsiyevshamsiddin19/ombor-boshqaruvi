"""2. Role-based queryset filtering: each trade sees only its own materials."""

from django.db import connection
from django.test import TestCase

from apps.inventory import selectors, services
from apps.inventory.models import MaterialCategory, MeasurementUnit
from apps.users.models import UserRole

from .factories import make_admin, make_material, make_user, stock_up


class RoleVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.mdf = make_material("MDF 16 mm")  # SHEET -> ARRACHI, ROVERCHI
        cls.paint = make_material(
            "Akril kraska", MaterialCategory.LIQUID, MeasurementUnit.LITER
        )  # LIQUID -> KRASKACHI
        cls.tape = make_material("PVX lenta 22 mm", MaterialCategory.LINEAR, MeasurementUnit.METER)  # SHKURKACHI, SBORKACHI
        cls.hinge = make_material("Petlya 35 mm", MaterialCategory.HARDWARE, MeasurementUnit.PIECE)  # SBORKACHI
        cls.special = make_material(
            "Shpon yong'oq", MaterialCategory.SHEET, allowed_roles=[UserRole.KRASKACHI]
        )  # admin override: veneer goes to the painters
        for material in (cls.mdf, cls.paint, cls.tape, cls.hinge, cls.special):
            stock_up(material, cls.admin, 20)

        cls.mdf_offcut = services.register_offcut(material=cls.mdf, user=cls.admin, length_mm=900, width_mm=400)
        cls.tape_offcut = services.register_offcut(material=cls.tape, user=cls.admin, length_mm=1500, width_mm=22)

    def names(self, user):
        return set(selectors.visible_whole_stock(user).values_list("material__name", flat=True))

    def test_backend_under_test(self):
        # Both code paths of roles_overlap_q are exercised: run the suite on PostgreSQL and on SQLite.
        self.assertIn(connection.vendor, {"postgresql", "sqlite"})

    def test_painter_sees_only_paint_materials(self):
        kraskachi = make_user("kraskachi", [UserRole.KRASKACHI])
        self.assertEqual(self.names(kraskachi), {"Akril kraska", "Shpon yong'oq"})

    def test_sawyer_sees_only_sheet_materials(self):
        arrachi = make_user("arrachi", [UserRole.ARRACHI])
        self.assertEqual(self.names(arrachi), {"MDF 16 mm"})

    def test_assembler_sees_tape_and_hardware(self):
        sborkachi = make_user("sborkachi", [UserRole.SBORKACHI])
        self.assertEqual(self.names(sborkachi), {"PVX lenta 22 mm", "Petlya 35 mm"})

    def test_sander_sees_only_linear_materials(self):
        shkurkachi = make_user("shkurkachi", [UserRole.SHKURKACHI])
        self.assertEqual(self.names(shkurkachi), {"PVX lenta 22 mm"})

    def test_multiple_roles_see_the_union(self):
        both = make_user("both", [UserRole.ROVERCHI, UserRole.KRASKACHI])
        self.assertEqual(self.names(both), {"MDF 16 mm", "Akril kraska", "Shpon yong'oq"})

    def test_admin_sees_everything(self):
        self.assertEqual(len(self.names(self.admin)), 5)

    def test_user_without_trade_roles_sees_nothing(self):
        nobody = make_user("nobody", [])
        self.assertEqual(self.names(nobody), set())
        self.assertFalse(selectors.visible_offcuts(nobody).exists())
        self.assertFalse(selectors.visible_materials(nobody).exists())

    def test_archived_materials_are_hidden_from_everyone(self):
        self.mdf.is_active = False
        self.mdf.save()
        arrachi = make_user("arrachi", [UserRole.ARRACHI])
        self.assertEqual(self.names(arrachi), set())
        self.assertNotIn("MDF 16 mm", self.names(self.admin))
        self.assertFalse(selectors.visible_offcuts(arrachi).exists())

    def test_offcuts_follow_the_parent_material_roles(self):
        arrachi = make_user("arrachi", [UserRole.ARRACHI])
        kraskachi = make_user("kraskachi", [UserRole.KRASKACHI])
        sborkachi = make_user("sborkachi", [UserRole.SBORKACHI])
        self.assertEqual(list(selectors.visible_offcuts(arrachi)), [self.mdf_offcut])
        self.assertEqual(list(selectors.visible_offcuts(kraskachi)), [])
        self.assertEqual(list(selectors.visible_offcuts(sborkachi)), [self.tape_offcut])
        self.assertEqual(selectors.visible_offcuts(self.admin).count(), 2)

    def test_offcut_capable_materials_exclude_liquids_and_hardware(self):
        sborkachi = make_user("sborkachi", [UserRole.SBORKACHI])
        kraskachi = make_user("kraskachi", [UserRole.KRASKACHI])
        self.assertEqual(list(selectors.offcut_capable_materials(sborkachi)), [self.tape])
        # The painter's veneer is a sheet, so it is the only offcut-capable material they have.
        self.assertEqual(list(selectors.offcut_capable_materials(kraskachi)), [self.special])

    def test_user_can_access_material(self):
        arrachi = make_user("arrachi", [UserRole.ARRACHI])
        pending = make_user("pending", [UserRole.ARRACHI], approved=False)
        self.assertTrue(selectors.user_can_access_material(arrachi, self.mdf))
        self.assertFalse(selectors.user_can_access_material(arrachi, self.paint))
        self.assertFalse(selectors.user_can_access_material(pending, self.mdf))
        self.assertTrue(selectors.user_can_access_material(self.admin, self.paint))

    def test_search_narrows_within_visible_stock_only(self):
        kraskachi = make_user("kraskachi", [UserRole.KRASKACHI])
        found = selectors.search_stock(selectors.visible_whole_stock(kraskachi), query="mdf")
        self.assertFalse(found.exists())
        found = selectors.search_stock(selectors.visible_whole_stock(kraskachi), category=MaterialCategory.LIQUID)
        self.assertEqual([s.material.name for s in found], ["Akril kraska"])

    def test_role_codes_are_matched_exactly(self):
        # A role must match a whole JSON token, never a fragment of another code or of the name.
        odd = make_material("ARRACHI uchun emas", MaterialCategory.LIQUID, MeasurementUnit.LITER)
        stock_up(odd, self.admin, 1)
        arrachi = make_user("arrachi", [UserRole.ARRACHI])
        self.assertNotIn("ARRACHI uchun emas", self.names(arrachi))
