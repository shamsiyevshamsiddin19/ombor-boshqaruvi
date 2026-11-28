"""“Boshqa” lets an admin name an expense type of their own (e.g. “Elektr”), offered in the list from then on."""

from django.test import TestCase
from django.urls import reverse

from apps.inventory import selectors, services
from apps.inventory.models import ExpenseCategory, OperatingExpense, expense_category_choices, normalize_expense_type

from .factories import make_admin


def post_data(**overrides):
    data = {
        "expense_date": "2026-11-28",
        "category": ExpenseCategory.OTHER,
        "new_category": "",
        "title": "Noyabr uchun to'lov",
        "notes": "",
        "amount_usd": "40.00",
    }
    data.update(overrides)
    return data


class CustomExpenseTypeTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.client.force_login(self.admin)
        self.url = reverse("inventory:expenses")

    def test_boshqa_with_a_name_saves_that_type(self):
        response = self.client.post(self.url, post_data(new_category="elektr"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OperatingExpense.objects.get().category, "Elektr")

    def test_boshqa_without_a_name_stays_boshqa(self):
        self.client.post(self.url, post_data())
        self.assertEqual(OperatingExpense.objects.get().category, ExpenseCategory.OTHER)

    def test_a_new_type_is_offered_next_time_and_shown_in_the_list(self):
        self.client.post(self.url, post_data(new_category="Elektr"))
        self.assertIn(("Elektr", "Elektr"), expense_category_choices())
        self.assertEqual(expense_category_choices()[-1][0], ExpenseCategory.OTHER)  # "Boshqa" stays last
        page = self.client.get(self.url, {"month": "2026-11"})
        self.assertContains(page, '<option value="Elektr"')
        self.assertContains(page, "Elektr")

    def test_same_type_is_not_kept_under_two_spellings(self):
        services.record_expense(user=self.admin, category="Elektr", title="Oktyabr", amount_usd="30")
        self.assertEqual(normalize_expense_type("  ELEKTR "), "Elektr")
        self.assertEqual(normalize_expense_type("nalog"), ExpenseCategory.NALOG)
        self.client.post(self.url, post_data(new_category="elektr"))
        self.assertEqual(set(OperatingExpense.objects.values_list("category", flat=True)), {"Elektr"})

    def test_name_field_is_ignored_unless_boshqa_is_chosen(self):
        self.client.post(self.url, post_data(category=ExpenseCategory.STANOK, new_category="Elektr"))
        self.assertEqual(OperatingExpense.objects.get().category, ExpenseCategory.STANOK)

    def test_filter_and_totals_include_own_types(self):
        services.record_expense(user=self.admin, category="Elektr", title="Svet", amount_usd="25")
        services.record_expense(user=self.admin, category=ExpenseCategory.NALOG, title="Soliq", amount_usd="10")
        self.assertEqual(selectors.filter_expenses(category="Elektr").count(), 1)
        totals = selectors.expense_totals()
        self.assertEqual(str(totals["other_usd"]), "25.00")  # own types are counted with "Boshqa"
        self.assertEqual(str(totals["nalog_usd"]), "10.00")

    def test_api_accepts_an_own_type(self):
        response = self.client.post(
            "/api/expenses/",
            {"category": "internet", "title": "Provayder", "amount_usd": "12.00", "expense_date": "2026-11-28"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["category"], "Internet")


class ExpenseDateFieldTests(TestCase):
    def test_date_input_is_prefilled_in_iso_format_in_every_language(self):
        self.client.force_login(make_admin())
        for lang in ("uz", "ru", "en"):
            self.client.cookies["django_language"] = lang
            page = self.client.get(reverse("inventory:expenses")).content.decode()
            self.assertRegex(page, r'name="expense_date" value="\d{4}-\d{2}-\d{2}"', lang)
