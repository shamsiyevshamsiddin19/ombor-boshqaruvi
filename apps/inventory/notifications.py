"""The administrator's bell menu.

Two kinds of entries share one list:

* **Stored** notifications (``Notification`` rows) are events such as "a material was added".
  Each administrator has their own copy and marks it read on their own.
* **Live** notifications (low stock, registrations waiting for approval) mirror the current state
  of the warehouse. Reading one hides it for that person's session until the number changes, so a
  new low-stock material brings it back.
"""

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.users.models import User

from . import selectors
from .models import Notification

#: Cap on how many stored entries the menu shows at once.
MENU_LIMIT = 20

SESSION_KEY = "dismissed_notices"
LIVE_KEYS = ("low_stock", "pending_users")


def admin_recipients():
    """Every active administrator, superusers included."""
    return [user for user in User.objects.filter(is_active=True) if user.is_warehouse_admin]


def notify_admins(*, kind: str, message: str, url: str = "") -> None:
    """Add one unread entry to every administrator's bell menu."""
    Notification.objects.bulk_create(
        [Notification(recipient=user, kind=kind, message=message[:300], url=url) for user in admin_recipients()]
    )


def _live_counts() -> dict[str, int]:
    return {
        "low_stock": selectors.low_stock_items().count(),
        "pending_users": User.objects.pending_approval().count(),
    }


def feed_for(user, session) -> list[dict]:
    """Unread entries for ``user``, newest stored events first after the live ones."""
    dismissed = session.get(SESSION_KEY, {})
    counts = _live_counts()
    items: list[dict] = []

    low = counts["low_stock"]
    if low and dismissed.get("low_stock") != low:
        items.append(
            {
                "key": "low_stock",
                "kind": "alert",
                "title": _("Minimal qoldiqdan kam"),
                "text": ngettext("%(n)d ta material kam qoldi", "%(n)d ta material kam qoldi", low) % {"n": low},
                "url": reverse("inventory:dashboard"),
            }
        )
    pending = counts["pending_users"]
    if pending and dismissed.get("pending_users") != pending:
        items.append(
            {
                "key": "pending_users",
                "kind": "mail",
                "title": _("Yangi so'rovlar"),
                "text": ngettext(
                    "%(n)d ta xodim tasdiq kutmoqda", "%(n)d ta xodim tasdiq kutmoqda", pending
                )
                % {"n": pending},
                "url": reverse("users:pending_users"),
            }
        )

    stored = Notification.objects.filter(recipient=user, read_at__isnull=True)[:MENU_LIMIT]
    for note in stored:
        items.append(
            {
                "key": f"n{note.pk}",
                "kind": note.kind,
                "title": "",
                "text": note.message,
                "url": note.url,
                "when": note.created_at,
            }
        )
    return items


def mark_read(user, session, key: str) -> bool:
    """Mark one entry read for ``user``. Returns False when ``key`` names nothing."""
    if key in LIVE_KEYS:
        dismissed = dict(session.get(SESSION_KEY, {}))
        dismissed[key] = _live_counts()[key]
        session[SESSION_KEY] = dismissed
        return True
    if key.startswith("n") and key[1:].isdigit():
        Notification.objects.filter(pk=int(key[1:]), recipient=user, read_at__isnull=True).update(
            read_at=timezone.now()
        )
        return True
    return False


def mark_all_read(user, session) -> None:
    Notification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=timezone.now())
    session[SESSION_KEY] = _live_counts()
