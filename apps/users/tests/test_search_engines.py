"""A search result should open the login page; sign-up and the app itself stay out of search engines."""

from django.test import TestCase
from django.urls import reverse


class SearchEngineTests(TestCase):
    def test_robots_txt_allows_only_the_login_page(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        body = response.content.decode()
        self.assertIn("Allow: /accounts/login/$", body)
        self.assertIn("Disallow: /", body)
        self.assertIn("Sitemap: http://testserver/sitemap.xml", body)

    def test_sitemap_lists_the_login_page(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<loc>http://testserver/accounts/login/</loc>")

    def test_login_page_is_indexable_with_one_canonical_address(self):
        page = self.client.get(reverse("users:login") + "?next=/")
        self.assertContains(page, '<meta name="robots" content="index, follow">')
        self.assertContains(page, '<link rel="canonical" href="http://testserver/accounts/login/">')
        self.assertContains(page, '<meta name="description"')

    def test_registration_page_is_kept_out_of_search_results(self):
        page = self.client.get(reverse("users:register"))
        self.assertContains(page, '<meta name="robots" content="noindex, nofollow">')

    def test_sign_up_link_is_blue(self):
        page = self.client.get(reverse("users:login")).content.decode()
        self.assertRegex(page, r'class="[^"]*text-blue-700[^"]*" href="/accounts/register/"')

    def test_visitors_from_a_search_land_on_login(self):
        self.assertRedirects(self.client.get("/"), reverse("users:login") + "?next=/")
