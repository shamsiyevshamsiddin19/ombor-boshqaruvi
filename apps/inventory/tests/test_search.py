"""Search boxes find a material or a worker by any part of the name, however the apostrophe is typed."""

from django.test import TestCase
from django.urls import reverse

from apps.inventory import selectors
from apps.inventory.models import BaseMaterial, MaterialCategory, MeasurementUnit
from apps.users.models import User, UserRole

from .factories import make_admin, make_material, make_user


class TextSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_admin()
        cls.ldsp = make_material("LDSP 18 mm yong'oq")
        cls.mdf = make_material("MDF 16 mm oq")
        cls.paint = make_material("Akril kraska oq", MaterialCategory.LIQUID, MeasurementUnit.LITER)
        cls.worker = make_user("bobur", [UserRole.ARRACHI], full_name="Bobur Karimov", phone="+998901112233")

    def names(self, query):
        return set(BaseMaterial.objects.filter(selectors.text_search_q("name", query)).values_list("name", flat=True))

    def test_middle_of_the_name_is_found(self):
        self.assertEqual(self.names("18 mm"), {"LDSP 18 mm yong'oq"})
        self.assertEqual(self.names("oq"), {"LDSP 18 mm yong'oq", "MDF 16 mm oq", "Akril kraska oq"})

    def test_case_and_apostrophe_do_not_matter(self):
        for query in ("yong'oq", "yong‘oq", "yongʻoq", "YONG’OQ"):
            self.assertEqual(self.names(query), {"LDSP 18 mm yong'oq"}, query)

    def test_a_single_letter_means_starts_with(self):
        self.assertEqual(self.names("l"), {"LDSP 18 mm yong'oq"})

    def test_warehouse_and_material_pages_use_it(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("inventory:home"), {"q": "16 mm"}), "MDF 16 mm oq")
        self.assertNotContains(self.client.get(reverse("inventory:home"), {"q": "16 mm"}), "LDSP 18 mm")
        page = self.client.get(reverse("inventory:material_list"), {"q": "kraska"})
        self.assertContains(page, "Akril kraska oq")

    def test_suggestions_put_names_starting_with_the_text_first(self):
        self.client.force_login(self.admin)
        names = self.client.get(reverse("inventory:material_suggestions"), {"q": "mdf"}).json()["names"]
        self.assertEqual(names, ["MDF 16 mm oq"])
        names = self.client.get(reverse("inventory:material_suggestions"), {"q": "ak"}).json()["names"]
        self.assertEqual(names[0], "Akril kraska oq")  # starts with "ak"; "kraska" also contains it, but later

    def test_worker_list_finds_a_surname_and_a_spaced_phone(self):
        self.client.force_login(self.admin)
        for query in ("Karimov", "bobur kar", "+998 90 111 22 33", "90 111"):
            page = self.client.get(reverse("users:user_list"), {"q": query})
            self.assertContains(page, "Bobur Karimov", msg_prefix=query)
        self.assertEqual(User.objects.count(), 2)


class PhoneInputTests(TestCase):
    def test_phone_fields_are_marked_for_live_formatting(self):
        self.assertContains(self.client.get(reverse("users:login")), "data-phone-mask")
        self.assertContains(self.client.get(reverse("users:register")), "data-phone-mask")
