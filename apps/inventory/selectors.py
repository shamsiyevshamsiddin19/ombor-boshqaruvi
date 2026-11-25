"""
Read-side queries: role-based visibility, valuation and reporting.

Every list a worker sees (HTML or API) goes through ``visible_*`` so that
material isolation by trade is enforced in exactly one place.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from django.db import connection
from django.db.models import BigIntegerField, Count, DecimalField, ExpressionWrapper, F, Q, QuerySet, Sum, Value
from django.db.models.functions import Cast, Coalesce, Round, TruncMonth
from django.utils import timezone

from .models import (
    BaseMaterial,
    ExpenseCategory,
    InventoryTransaction,
    LeftoverOffcut,
    OperatingExpense,
    TransactionType,
    WholeMaterial,
    offcut_category_codes,
)

MONEY = DecimalField(max_digits=24, decimal_places=6)
ZERO_MONEY = Value(Decimal("0"), output_field=MONEY)


# ---------------------------------------------------------------------------
# Role-based visibility
# ---------------------------------------------------------------------------
def roles_overlap_q(field: str, roles: list[str]) -> Q:
    """
    Q matching rows whose JSON list ``field`` shares at least one value with ``roles``.

    PostgreSQL uses the indexable jsonb containment operator (``@>``). Other
    backends (SQLite for quick local runs) fall back to matching the quoted
    token inside the serialised JSON, which is unambiguous for our role codes.
    """
    if not roles:
        return Q(pk__in=[])
    query = Q()
    for role in roles:
        if connection.vendor == "postgresql":
            query |= Q(**{f"{field}__contains": [role]})
        else:
            query |= Q(**{f"{field}__icontains": f'"{role}"'})
    return query


def visible_materials(user) -> QuerySet[BaseMaterial]:
    """Active materials the user may view and withdraw. Admins see everything active."""
    qs = BaseMaterial.objects.filter(is_active=True)
    if user.is_warehouse_admin:
        return qs
    roles = user.worker_roles
    if not roles:
        return qs.none()
    return qs.filter(roles_overlap_q("allowed_roles", roles))


def visible_whole_stock(user) -> QuerySet[WholeMaterial]:
    qs = WholeMaterial.objects.select_related("material").filter(material__is_active=True)
    if user.is_warehouse_admin:
        return qs
    roles = user.worker_roles
    if not roles:
        return qs.none()
    return qs.filter(roles_overlap_q("material__allowed_roles", roles))


def visible_offcuts(user) -> QuerySet[LeftoverOffcut]:
    qs = LeftoverOffcut.objects.select_related("material", "material__whole_stock", "created_by").filter(
        material__is_active=True
    )
    if user.is_warehouse_admin:
        return qs
    roles = user.worker_roles
    if not roles:
        return qs.none()
    return qs.filter(roles_overlap_q("material__allowed_roles", roles))


def offcut_capable_materials(user) -> QuerySet[BaseMaterial]:
    return visible_materials(user).filter(category__in=offcut_category_codes())


def user_can_access_material(user, material: BaseMaterial) -> bool:
    if not getattr(user, "can_use_system", False):
        return False
    if user.is_warehouse_admin:
        return True
    return material.is_active and bool(set(material.allowed_roles) & set(user.worker_roles))


def search_stock(qs: QuerySet, *, query: str = "", category: str = "") -> QuerySet:
    if query:
        qs = qs.filter(material__name__icontains=query)
    if category:
        qs = qs.filter(material__category=category)
    return qs


# ---------------------------------------------------------------------------
# Valuation & alerts
# ---------------------------------------------------------------------------
def warehouse_valuation_usd() -> Decimal:
    """
    Sum of WholeMaterial.quantity x BaseMaterial.unit_price_usd, in USD.

    Each line is rounded to cents before summing, so the total always equals the
    sum of the per-material values shown in the tables.
    """
    line_value = Round(ExpressionWrapper(F("quantity") * F("material__unit_price_usd"), output_field=MONEY), 2)
    total = WholeMaterial.objects.aggregate(total=Coalesce(Sum(line_value, output_field=MONEY), ZERO_MONEY))["total"]
    return Decimal(total).quantize(Decimal("0.01"))


def low_stock_items() -> QuerySet[WholeMaterial]:
    """Active materials whose quantity dropped below the configured minimum threshold."""
    return (
        WholeMaterial.objects.select_related("material")
        .filter(material__is_active=True, quantity__lt=F("material__minimum_threshold"))
        .order_by("material__category", "material__name")
    )


def estimated_offcut_value_usd() -> Decimal:
    """
    Informational: recoverable value sitting in offcuts (not part of the valuation).

    Calculated via database-level SQL aggregation to avoid loading offcut records
    into application memory.
    """
    BIGINT = BigIntegerField()
    line_val = Round(
        ExpressionWrapper(
            (
                Cast(F("length_mm"), BIGINT)
                * F("width_mm")
                * F("material__unit_price_usd")
                * F("quantity")
                * Value(Decimal("1.000000"), output_field=MONEY)
            )
            / (Cast(F("material__whole_stock__length_mm"), BIGINT) * F("material__whole_stock__width_mm")),
            output_field=MONEY,
        ),
        2,
    )
    total = (
        LeftoverOffcut.objects.filter(
            material__whole_stock__length_mm__isnull=False,
            material__whole_stock__width_mm__isnull=False,
            material__whole_stock__length_mm__gt=0,
            material__whole_stock__width_mm__gt=0,
        ).aggregate(total=Coalesce(Sum(line_val, output_field=MONEY), ZERO_MONEY))["total"]
    )
    return Decimal(total).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Monthly flow & history
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MonthFlow:
    month: dt.date
    kirim_usd: Decimal
    chiqim_usd: Decimal
    kirim_count: int
    chiqim_count: int

    @property
    def net_usd(self) -> Decimal:
        return self.kirim_usd - self.chiqim_usd


def month_bounds(year: int, month: int) -> tuple[dt.datetime, dt.datetime]:
    tz = timezone.get_current_timezone()
    start = dt.datetime(year, month, 1, tzinfo=tz)
    end = dt.datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
    return start, end


def _flow_aggregates(qs: QuerySet[InventoryTransaction]) -> dict:
    return qs.aggregate(
        kirim_usd=Coalesce(Sum("total_price_usd", filter=Q(transaction_type=TransactionType.KIRIM)), ZERO_MONEY),
        chiqim_usd=Coalesce(Sum("total_price_usd", filter=Q(transaction_type=TransactionType.CHIQIM)), ZERO_MONEY),
        kirim_count=Count("id", filter=Q(transaction_type=TransactionType.KIRIM)),
        chiqim_count=Count("id", filter=Q(transaction_type=TransactionType.CHIQIM)),
    )


def monthly_flow(year: int, month: int) -> MonthFlow:
    start, end = month_bounds(year, month)
    data = _flow_aggregates(InventoryTransaction.objects.filter(created_at__gte=start, created_at__lt=end))
    return MonthFlow(
        month=dt.date(year, month, 1),
        kirim_usd=Decimal(data["kirim_usd"]).quantize(Decimal("0.01")),
        chiqim_usd=Decimal(data["chiqim_usd"]).quantize(Decimal("0.01")),
        kirim_count=data["kirim_count"],
        chiqim_count=data["chiqim_count"],
    )


def recent_month_flows(months: int = 6, *, today: dt.date | None = None) -> list[MonthFlow]:
    """Kirim vs Chiqim totals for the last ``months`` calendar months, oldest first."""
    today = today or timezone.localdate()
    year, month = today.year, today.month
    keys: list[tuple[int, int]] = []
    for _ in range(months):
        keys.append((year, month))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    keys.reverse()

    start, _ = month_bounds(*keys[0])
    _, end = month_bounds(*keys[-1])
    rows = (
        InventoryTransaction.objects.filter(created_at__gte=start, created_at__lt=end)
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(
            kirim_usd=Coalesce(Sum("total_price_usd", filter=Q(transaction_type=TransactionType.KIRIM)), ZERO_MONEY),
            chiqim_usd=Coalesce(Sum("total_price_usd", filter=Q(transaction_type=TransactionType.CHIQIM)), ZERO_MONEY),
            kirim_count=Count("id", filter=Q(transaction_type=TransactionType.KIRIM)),
            chiqim_count=Count("id", filter=Q(transaction_type=TransactionType.CHIQIM)),
        )
    )
    by_month = {}
    for row in rows:
        month_value = row["month"]
        if isinstance(month_value, dt.datetime):
            month_value = timezone.localtime(month_value).date() if timezone.is_aware(month_value) else month_value.date()
        by_month[(month_value.year, month_value.month)] = row

    flows = []
    for key in keys:
        row = by_month.get(key, {})
        flows.append(
            MonthFlow(
                month=dt.date(key[0], key[1], 1),
                kirim_usd=Decimal(row.get("kirim_usd", 0)).quantize(Decimal("0.01")),
                chiqim_usd=Decimal(row.get("chiqim_usd", 0)).quantize(Decimal("0.01")),
                kirim_count=row.get("kirim_count", 0),
                chiqim_count=row.get("chiqim_count", 0),
            )
        )
    return flows


# ---------------------------------------------------------------------------
# Transaction history / reports
# ---------------------------------------------------------------------------
def filter_transactions(
    qs: QuerySet[InventoryTransaction] | None = None,
    *,
    month: dt.date | None = None,
    category: str = "",
    user_id: int | None = None,
    material_id: int | None = None,
    transaction_type: str = "",
) -> QuerySet[InventoryTransaction]:
    qs = qs if qs is not None else InventoryTransaction.objects.all()
    qs = qs.select_related("material", "user")
    if month:
        start, end = month_bounds(month.year, month.month)
        qs = qs.filter(created_at__gte=start, created_at__lt=end)
    if category:
        qs = qs.filter(material__category=category)
    if user_id:
        qs = qs.filter(user_id=user_id)
    if material_id:
        qs = qs.filter(material_id=material_id)
    if transaction_type:
        qs = qs.filter(transaction_type=transaction_type)
    return qs


def transaction_totals(qs: QuerySet[InventoryTransaction]) -> dict:
    data = _flow_aggregates(qs.order_by())
    data["offcut_count"] = qs.filter(transaction_type=TransactionType.OFFCUT_DEPOSIT).count()
    data["kirim_usd"] = Decimal(data["kirim_usd"]).quantize(Decimal("0.01"))
    data["chiqim_usd"] = Decimal(data["chiqim_usd"]).quantize(Decimal("0.01"))
    data["net_usd"] = data["kirim_usd"] - data["chiqim_usd"]
    data["net_abs"] = abs(data["net_usd"])
    return data


def consumption_by_material(qs: QuerySet[InventoryTransaction]) -> QuerySet:
    """Chiqim aggregated per material: how much of each material the workshop used."""
    return (
        qs.filter(transaction_type=TransactionType.CHIQIM)
        .order_by()
        .values("material_id", "material__name", "material__category", "material__unit_of_measure")
        .annotate(
            quantity=Sum("quantity_changed"),
            total_usd=Coalesce(Sum("total_price_usd"), ZERO_MONEY),
            entries=Count("id"),
        )
        .order_by("-total_usd", "material__name")
    )


def consumption_by_worker(qs: QuerySet[InventoryTransaction]) -> QuerySet:
    return (
        qs.filter(transaction_type=TransactionType.CHIQIM)
        .order_by()
        .values("user_id", "user__username", "user__full_name")
        .annotate(total_usd=Coalesce(Sum("total_price_usd"), ZERO_MONEY), entries=Count("id"))
        .order_by("-total_usd")
    )


# ---------------------------------------------------------------------------
# Operating expenses (TZ 2.3: nalog, produkta, stanok)
# ---------------------------------------------------------------------------
def filter_expenses(
    qs: QuerySet[OperatingExpense] | None = None,
    *,
    month: dt.date | None = None,
    category: str = "",
) -> QuerySet[OperatingExpense]:
    qs = qs if qs is not None else OperatingExpense.objects.all()
    qs = qs.select_related("created_by")
    if month:
        start_date = dt.date(month.year, month.month, 1)
        end_date = dt.date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        qs = qs.filter(expense_date__gte=start_date, expense_date__lt=end_date)
    if category:
        qs = qs.filter(category=category)
    return qs


def expense_totals(qs: QuerySet[OperatingExpense] | None = None) -> dict:
    qs = qs if qs is not None else OperatingExpense.objects.all()
    agg = qs.order_by().aggregate(
        total_usd=Coalesce(Sum("amount_usd"), ZERO_MONEY),
        count=Count("id"),
        nalog_usd=Coalesce(Sum("amount_usd", filter=Q(category=ExpenseCategory.NALOG)), ZERO_MONEY),
        produkta_usd=Coalesce(Sum("amount_usd", filter=Q(category=ExpenseCategory.PRODUKTA)), ZERO_MONEY),
        stanok_usd=Coalesce(Sum("amount_usd", filter=Q(category=ExpenseCategory.STANOK)), ZERO_MONEY),
        other_usd=Coalesce(Sum("amount_usd", filter=Q(category=ExpenseCategory.OTHER)), ZERO_MONEY),
    )
    return {
        "total_usd": Decimal(agg["total_usd"]).quantize(Decimal("0.01")),
        "count": agg["count"],
        "nalog_usd": Decimal(agg["nalog_usd"]).quantize(Decimal("0.01")),
        "produkta_usd": Decimal(agg["produkta_usd"]).quantize(Decimal("0.01")),
        "stanok_usd": Decimal(agg["stanok_usd"]).quantize(Decimal("0.01")),
        "other_usd": Decimal(agg["other_usd"]).quantize(Decimal("0.01")),
    }


def monthly_expense_totals(year: int, month: int) -> dict:
    return expense_totals(filter_expenses(month=dt.date(year, month, 1)))
