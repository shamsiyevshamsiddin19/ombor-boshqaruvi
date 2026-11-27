from django.conf import settings
from django.utils.functional import SimpleLazyObject

from apps.users.models import User
from apps.users.permissions import is_warehouse_admin

from . import notifications, selectors

#: url_name -> the nav item that should be highlighted
NAV_SECTIONS = {
    "home": "home",
    "withdraw_whole": "home",
    "offcut_withdraw": "home",
    "offcut_add": "offcut_add",
    "my_history": "my_history",
    "reverse_transaction": "my_history",
    "dashboard": "dashboard",
    "kirim": "kirim",
    "material_list": "materials",
    "material_create": "materials",
    "material_edit": "materials",
    "expenses": "expenses",
    "expenses_export": "expenses",
    "reports": "reports",
    "pending_users": "pending_users",
    "pending_user_review": "pending_users",
    "user_list": "users",
    "user_edit": "users",
    "worker_role_create": "users",
}


def navigation(request):
    """Header/nav data. Admin-only figures are lazy so pages that skip them cost nothing."""
    match = getattr(request, "resolver_match", None)
    context = {
        "brand_name": settings.WMS_BRAND_NAME,
        "nav_section": NAV_SECTIONS.get(match.url_name if match else "", ""),
    }
    user = getattr(request, "user", None)
    if user is not None and is_warehouse_admin(user):
        context.update(
            {
                "nav_pending_users": SimpleLazyObject(lambda: User.objects.pending_approval().count()),
                "nav_notifications": SimpleLazyObject(lambda: notifications.feed_for(user, request.session)),
                "nav_valuation_usd": SimpleLazyObject(selectors.warehouse_valuation_usd),
            }
        )
    return context
