from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext, pgettext_lazy

from apps.users.models import UserRole, normalize_roles, role_label, worker_role_codes

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")


class MaterialCategory(models.TextChoices):
    """Legacy built-in category codes kept for compatibility alongside DB ``Category`` rows."""

    SHEET = "SHEET", pgettext_lazy("category", "List materiallar")
    LIQUID = "LIQUID", pgettext_lazy("category", "Bo'yoq va suyuqliklar")
    LINEAR = "LINEAR", pgettext_lazy("category", "Lenta va rulonlar")
    HARDWARE = "HARDWARE", pgettext_lazy("category", "Furnitura")


#: Seeded TZ category names declared here so i18n extract registers them under "category".
BUILTIN_TZ_CATEGORY_NAMES = (
    pgettext_lazy("category", "Kraska"),
    pgettext_lazy("category", "Gruntofka"),
    pgettext_lazy("category", "LMDF"),
    pgettext_lazy("category", "LDSP"),
    pgettext_lazy("category", "Faner"),
    pgettext_lazy("category", "PVC"),
    pgettext_lazy("category", "DSP"),
    pgettext_lazy("category", "Upakovka"),
)


class MeasurementUnit(models.TextChoices):
    PIECE = "PIECE", pgettext_lazy("unit", "Dona")
    SQM = "SQM", pgettext_lazy("unit", "Kvadrat metr")
    LITER = "LITER", pgettext_lazy("unit", "Litr")
    KILOGRAM = "KG", pgettext_lazy("unit", "Kilogramm")
    METER = "METER", pgettext_lazy("unit", "Metr")
    CENTIMETER = "CM", pgettext_lazy("unit", "Santimetr")


#: Short unit labels for compact tables and buttons.
UNIT_SHORT_LABELS = {
    MeasurementUnit.SQM: pgettext_lazy("unit short", "m²"),
    MeasurementUnit.LITER: pgettext_lazy("unit short", "L"),
    MeasurementUnit.KILOGRAM: pgettext_lazy("unit short", "kg"),
    MeasurementUnit.METER: pgettext_lazy("unit short", "m"),
    MeasurementUnit.CENTIMETER: pgettext_lazy("unit short", "sm"),
    MeasurementUnit.PIECE: pgettext_lazy("unit short", "dona"),
}

#: Units that can only move in whole numbers (you cannot take 1.5 hinges).
INTEGER_UNITS = frozenset({MeasurementUnit.PIECE})

#: Default trade access per legacy category code; DB ``Category.default_roles`` takes precedence.
DEFAULT_CATEGORY_ROLES: dict[str, list[str]] = {
    MaterialCategory.SHEET: [UserRole.ARRACHI, UserRole.ROVERCHI],
    MaterialCategory.LIQUID: [UserRole.KRASKACHI],
    MaterialCategory.LINEAR: [UserRole.SHKURKACHI, UserRole.SBORKACHI],
    MaterialCategory.HARDWARE: [UserRole.SBORKACHI],
}

#: Legacy categories supporting dimensions / offcuts; DB ``Category.supports_offcuts`` takes precedence.
OFFCUT_CATEGORIES = frozenset({MaterialCategory.SHEET, MaterialCategory.LINEAR})


def unit_short_label(unit: str) -> str:
    """'LITER' -> 'L' (or 'л' in Russian); unknown units are returned as they are."""
    label = UNIT_SHORT_LABELS.get(unit)
    return str(label) if label is not None else unit


class Category(models.Model):
    """A material category. Initial TZ categories are seeded; admins add more via "Boshqa"."""

    code = models.CharField(_("Kod"), max_length=50, unique=True)
    name = models.CharField(_("Nomi"), max_length=60)
    sort_order = models.PositiveIntegerField(_("Tartib"), default=100)
    supports_offcuts = models.BooleanField(_("O'lcham va qoldiq yuritiladi"), default=False)
    default_roles = models.JSONField(_("Standart kasblar"), default=list, blank=True)
    is_active = models.BooleanField(_("Faol"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("kategoriya")
        verbose_name_plural = _("kategoriyalar")

    def __str__(self) -> str:
        return self.name


# Per-process category cache, cached thread-safely via django.core.cache.
CATEGORY_CACHE_KEY = "wms:category_meta"


def clear_category_cache(*args, **kwargs) -> None:
    cache.delete(CATEGORY_CACHE_KEY)


post_save.connect(clear_category_cache, sender=Category)
post_delete.connect(clear_category_cache, sender=Category)


def localized_category_name(name: str) -> str:
    """Built-in category names are translated; names admins typed are shown as typed."""
    return pgettext("category", name) if name else name


def _category_rows() -> list[dict]:
    rows = cache.get(CATEGORY_CACHE_KEY)
    if rows is None:
        rows = list(Category.objects.values("code", "name", "supports_offcuts", "default_roles", "is_active"))
        cache.set(CATEGORY_CACHE_KEY, rows, timeout=300)
    return rows


def category_choices(*, include_inactive: bool = True, include_code: str = "") -> list[tuple[str, str]]:
    """Categories as (code, name) pairs in the current language. Usable as a form ``choices`` callable."""
    return [
        (row["code"], localized_category_name(row["name"]))
        for row in _category_rows()
        if include_inactive or row["is_active"] or (include_code and row["code"] == include_code)
    ]


def active_category_choices(include_code: str = "") -> list[tuple[str, str]]:
    return category_choices(include_inactive=False, include_code=include_code)


def category_codes() -> list[str]:
    return [row["code"] for row in _category_rows()]


def offcut_category_codes() -> set[str]:
    db_codes = {row["code"] for row in _category_rows() if row["supports_offcuts"]}
    return db_codes | OFFCUT_CATEGORIES


def default_roles_for_category(category: str) -> list[str]:
    for row in _category_rows():
        if row["code"] == category and row["default_roles"]:
            return [str(role) for role in row["default_roles"]]
    return [str(role) for role in DEFAULT_CATEGORY_ROLES.get(category, [])]


def category_label(code: str) -> str:
    return dict(category_choices()).get(code, code)  # already in the current language


def validate_category_code(value: str) -> None:
    if value not in category_codes():
        raise ValidationError(_("Noma'lum kategoriya: %(code)s"), params={"code": value})


def validate_worker_role_list(value) -> None:
    if not isinstance(value, list):
        raise ValidationError(_("Ruxsat etilgan kasblar ro'yxat (list) bo'lishi kerak."))
    trades = set(worker_role_codes())
    unknown = [role for role in value if role not in trades]
    if unknown:
        raise ValidationError(_("Noma'lum kasb(lar): %(roles)s"), params={"roles": ", ".join(map(str, unknown))})


class BaseMaterial(models.Model):
    name = models.CharField(_("Nomi"), max_length=255)
    category = models.CharField(_("Kategoriya"), max_length=50, validators=[validate_category_code])
    allowed_roles = models.JSONField(
        _("Ruxsat etilgan kasblar"),
        default=list,
        blank=True,
        validators=[validate_worker_role_list],
        help_text="List of UserRoles permitted to view/deduct this material",
    )
    unit_of_measure = models.CharField(_("O'lchov birligi"), max_length=20, choices=MeasurementUnit.choices)
    container_size = models.DecimalField(
        _("Idish hajmi"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Size per container (e.g., 12L or 25L bucket)",
    )

    # Stock thresholds
    minimum_threshold = models.DecimalField(
        _("Minimal qoldiq"),
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    # Price tracking (USD). Maintained as a weighted average cost by Kirim entries.
    unit_price_usd = models.DecimalField(
        _("Birlik narxi (USD)"),
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.0000"),
        validators=[MinValueValidator(Decimal("0.0000"))],
    )

    is_active = models.BooleanField(
        _("Faol"), default=True, help_text="Archived materials are hidden from workers but keep their history."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("material")
        verbose_name_plural = _("materiallar")
        constraints = [
            models.CheckConstraint(condition=models.Q(unit_price_usd__gte=0), name="material_price_non_negative"),
            models.CheckConstraint(condition=models.Q(minimum_threshold__gte=0), name="material_threshold_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.category})"

    def save(self, *args, **kwargs):
        self.allowed_roles = normalize_roles(self.allowed_roles)
        super().save(*args, **kwargs)

    @property
    def category_short(self) -> str:
        return category_label(self.category)

    @property
    def unit_short(self) -> str:
        return unit_short_label(self.unit_of_measure)

    @property
    def requires_integer_quantity(self) -> bool:
        return self.unit_of_measure in INTEGER_UNITS

    @property
    def supports_offcuts(self) -> bool:
        return self.category in offcut_category_codes()

    @property
    def allowed_role_labels(self) -> list[str]:
        return [role_label(role) for role in self.allowed_roles]


class WholeMaterial(models.Model):
    material = models.OneToOneField(BaseMaterial, on_delete=models.CASCADE, related_name="whole_stock")
    quantity = models.DecimalField(
        _("Miqdor"),
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    length_mm = models.IntegerField(_("Uzunligi (mm)"), null=True, blank=True, help_text="Standard sheet length in mm")
    width_mm = models.IntegerField(_("Eni (mm)"), null=True, blank=True, help_text="Standard sheet width in mm")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["material__name"]
        verbose_name = _("butun material")
        verbose_name_plural = _("butun materiallar")
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=0), name="whole_stock_non_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.material.name}: {self.quantity} {self.material.unit_short}"

    @property
    def total_value_usd(self) -> Decimal:
        return (self.quantity * self.material.unit_price_usd).quantize(CENTS, rounding=ROUND_HALF_UP)

    @property
    def is_below_threshold(self) -> bool:
        return self.quantity < self.material.minimum_threshold

    @property
    def shortfall(self) -> Decimal:
        return max(self.material.minimum_threshold - self.quantity, ZERO)

    @property
    def container_count(self) -> Decimal | None:
        size = self.material.container_size
        if not size:
            return None
        return (self.quantity / size).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    @property
    def sheet_area_mm2(self) -> int | None:
        if self.length_mm and self.width_mm:
            return self.length_mm * self.width_mm
        return None


class LeftoverOffcut(models.Model):
    material = models.ForeignKey(BaseMaterial, on_delete=models.CASCADE, related_name="offcuts")
    length_mm = models.IntegerField(_("Uzunligi (mm)"), validators=[MinValueValidator(1)], help_text="Remaining length in mm")
    width_mm = models.IntegerField(_("Eni (mm)"), validators=[MinValueValidator(1)], help_text="Remaining width in mm")
    quantity = models.IntegerField(_("Soni"), default=1, validators=[MinValueValidator(1)])
    notes = models.TextField(_("Izoh"), blank=True, help_text="Location or description of offcut scrap")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="registered_offcuts"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("qoldiq material")
        verbose_name_plural = _("qoldiq materiallar")
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="offcut_quantity_positive"),
            models.CheckConstraint(
                condition=models.Q(length_mm__gte=1, width_mm__gte=1), name="offcut_dimensions_positive"
            ),
        ]
        indexes = [models.Index(fields=["material", "-created_at"])]

    def __str__(self) -> str:
        return f"Offcut: {self.material.name} ({self.length_mm}x{self.width_mm} mm) - Qty: {self.quantity}"

    @property
    def area_m2(self) -> Decimal:
        return (Decimal(self.length_mm * self.width_mm) / Decimal(1_000_000)).quantize(Decimal("0.001"))

    @property
    def estimated_value_usd(self) -> Decimal | None:
        """
        Informational only: area share of a full sheet x current unit price.

        Offcuts are *not* part of the warehouse valuation; their cost was already
        booked when the parent sheet was withdrawn (Chiqim).
        """
        stock = getattr(self.material, "whole_stock", None)
        if stock is None or not stock.sheet_area_mm2:
            return None
        share = Decimal(self.length_mm * self.width_mm) / Decimal(stock.sheet_area_mm2)
        value = share * self.material.unit_price_usd * self.quantity
        return value.quantize(CENTS, rounding=ROUND_HALF_UP)


class TransactionType(models.TextChoices):
    KIRIM = "KIRIM", "Stock Arrival (Kirim)"
    CHIQIM = "CHIQIM", "Worker Withdrawal (Chiqim)"
    OFFCUT_DEPOSIT = "OFFCUT_DEPOSIT", "Offcut Deposit (Qoldiq)"


#: Labels shown in the app (report filter, CSV). The English choice labels above are for Django admin only.
TRANSACTION_TYPE_LABELS = {
    TransactionType.KIRIM: _("Kirim"),
    TransactionType.CHIQIM: _("Chiqim"),
    TransactionType.OFFCUT_DEPOSIT: _("Qoldiq qo'shildi"),
}


def transaction_type_choices() -> list[tuple[str, str]]:
    return [(value, str(TRANSACTION_TYPE_LABELS[value])) for value in TransactionType.values]


def transaction_type_label(value: str) -> str:
    label = TRANSACTION_TYPE_LABELS.get(value)
    return str(label) if label is not None else value


class ImmutableLedgerError(Exception):
    """Raised on any attempt to modify or delete an inventory ledger entry."""


class InventoryTransactionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableLedgerError("Inventory transactions are append-only and cannot be updated.")

    def delete(self):
        raise ImmutableLedgerError("Inventory transactions are append-only and cannot be deleted.")

    def kirim(self):
        return self.filter(transaction_type=TransactionType.KIRIM)

    def chiqim(self):
        return self.filter(transaction_type=TransactionType.CHIQIM)


class InventoryTransaction(models.Model):
    """
    Append-only ledger entry.

    Sign convention: ``quantity_changed`` is a signed delta (+ for Kirim and offcut
    deposits, - for Chiqim). ``total_price_usd`` is always the non-negative USD value
    of the movement. Offcut movements carry a value of $0 because the cost of the
    parent sheet is booked when the whole sheet is withdrawn.
    """

    transaction_type = models.CharField(max_length=20, choices=TransactionType.choices)
    # PROTECT (not CASCADE): deleting a material must never erase ledger history.
    material = models.ForeignKey(BaseMaterial, on_delete=models.PROTECT, related_name="transactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="transactions")

    quantity_changed = models.DecimalField(max_digits=12, decimal_places=2)
    unit_price_usd = models.DecimalField(max_digits=12, decimal_places=4)
    total_price_usd = models.DecimalField(max_digits=14, decimal_places=2)
    stock_after = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Whole-material balance right after this entry (null for offcut movements).",
    )

    is_offcut = models.BooleanField(default=False)
    offcut_length_mm = models.IntegerField(null=True, blank=True)
    offcut_width_mm = models.IntegerField(null=True, blank=True)

    idempotency_key = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        help_text="Client-supplied key to prevent duplicate submissions.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = InventoryTransactionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("ombor harakati")
        verbose_name_plural = _("ombor harakatlari")
        indexes = [
            models.Index(fields=["transaction_type", "created_at"]),
            models.Index(fields=["material", "created_at"]),
            models.Index(fields=["user", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(total_price_usd__gte=0), name="txn_total_non_negative"),
            models.CheckConstraint(condition=models.Q(unit_price_usd__gte=0), name="txn_unit_price_non_negative"),
            models.CheckConstraint(
                condition=(
                    models.Q(transaction_type=TransactionType.CHIQIM, quantity_changed__lt=0)
                    | (~models.Q(transaction_type=TransactionType.CHIQIM) & models.Q(quantity_changed__gt=0))
                ),
                name="txn_quantity_sign_matches_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_transaction_type_display()} · {self.material.name} · {self.quantity_changed}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableLedgerError("Inventory transactions are append-only and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableLedgerError("Inventory transactions are append-only and cannot be deleted.")

    @property
    def quantity_abs(self) -> Decimal:
        return abs(self.quantity_changed)


# ---------------------------------------------------------------------------
# Extra operating expenses (TZ 2.3: nalog, produkta, stanok, etc.)
# ---------------------------------------------------------------------------
class ExpenseCategory(models.TextChoices):
    NALOG = "NALOG", pgettext_lazy("expense_category", "Nalog")
    PRODUKTA = "PRODUKTA", pgettext_lazy("expense_category", "Produkta")
    STANOK = "STANOK", pgettext_lazy("expense_category", "Stanok")
    OTHER = "OTHER", pgettext_lazy("expense_category", "Boshqa")


def expense_category_choices() -> list[tuple[str, str]]:
    return [(choice.value, str(choice.label)) for choice in ExpenseCategory]


def expense_category_label(value: str) -> str:
    try:
        return str(ExpenseCategory(value).label)
    except ValueError:
        return value


class OperatingExpense(models.Model):
    """Additional factory/operating expense (nalog, produkta, stanok, etc.)."""

    category = models.CharField(_("Turi"), max_length=30, choices=ExpenseCategory.choices)
    title = models.CharField(_("Xarajat nomi"), max_length=200)
    amount_usd = models.DecimalField(
        _("Summa (USD $)"),
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    expense_date = models.DateField(_("Sana"), default=timezone.localdate, db_index=True)
    notes = models.TextField(_("Izoh"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="operating_expenses"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-expense_date", "-created_at", "-id"]
        verbose_name = _("qo'shimcha xarajat")
        verbose_name_plural = _("qo'shimcha xarajatlar")
        indexes = [
            models.Index(fields=["expense_date", "category"]),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_usd__gt=0), name="expense_amount_positive"),
        ]

    def __str__(self) -> str:
        return f"{expense_category_label(self.category)} · {self.title} · ${self.amount_usd}"
