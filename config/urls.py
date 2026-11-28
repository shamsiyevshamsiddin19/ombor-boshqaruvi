from django.contrib import admin
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.urls import include, path, reverse
from django.utils.html import escape
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

admin.site.site_header = _("Mebel Ombori — texnik boshqaruv")


def robots_txt(request):
    """Only the login page (and the root, which redirects to it) may appear in search results."""
    login = reverse("users:login")
    lines = [
        "User-agent: *",
        "Allow: /$",
        f"Allow: {login}$",
        "Disallow: /",
        f"Sitemap: {request.build_absolute_uri(reverse('sitemap'))}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


def sitemap_xml(request):
    login = request.build_absolute_uri(reverse("users:login"))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{escape(login)}</loc></url>\n"
        "</urlset>\n"
    )
    return HttpResponse(body, content_type="application/xml; charset=utf-8")


def healthz(request):
    try:
        connection.ensure_connection()
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "error", "detail": "database"}, status=503)


urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("robots.txt", robots_txt, name="robots_txt"),
    path("sitemap.xml", sitemap_xml, name="sitemap"),
    # Django's built-in admin is kept for superusers / data fixes; day-to-day
    # administration happens in the app's own /admin-dashboard/ pages.
    path("django-admin/", admin.site.urls),
    path("api/", include("config.api_urls")),
    # POST /i18n/setlang/ with `language` switches the interface language.
    path("i18n/", include("django.conf.urls.i18n")),
    path("theme-preview/", TemplateView.as_view(template_name="theme_preview.html"), name="theme_preview"),
    path("", include("apps.users.urls")),
    path("", include("apps.inventory.urls")),
]
