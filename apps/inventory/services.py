"""
Write-side business operations. Every stock movement goes through here so that
locking, validation and ledger entries are applied consistently for the HTML
views, the REST API and management commands alike.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import (
    BaseMaterial,
    ExpenseCategory,
    InventoryTransaction,
    LeftoverOffcut,
    OperatingExpense,
    TransactionType,
    WholeMaterial,
    default_roles_for_category,
    offcut_category_codes,
)
from .selectors import user_can_access_material

logger = logging.getLogger("wms.security")

CENTS = Decimal("0.01")
PRICE_PLACES = Decimal("0.0001")
ZERO = Decimal("0")

MAX_QUANTITY = Decimal("9999999999.99")
MAX_TOTAL_PRICE = Decimal("999999999999.99")
MAX_UNIT_PRICE = Decimal("99999999.9999")


class InsufficientStockError(ValidationError):
    """Requested quantity exceeds what is on the shelf."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _decimal(value, field: str) -> Decimal:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: _("Noto'g'ri raqam.")}) from exc
    if not number.is_finite():
        raise ValidationError({field: _("Noto'g'ri raqam.")})
    return number


def _positive_quantity(material: BaseMaterial, value, field: str = "quantity") -> Decimal:
    quantity = _decimal(value, field)
    if quantity <= 0:
        raise ValidationError({field: _("Miqdor 0 dan katta bo'lishi kerak.")})
    if quantity != quantity.quantize(CENTS):
        raise ValidationError({field: _("Miqdor ko'pi bilan 2 xonali kasr bo'lishi mumkin.")})
    if material.requires_integer_quantity and quantity != quantity.to_integral_value():
        raise ValidationError(
            {field: _("%(name)s faqat butun sonda hisoblanadi (%(unit)s).") % {
                "name": material.name, "unit": material.unit_short}}
        )
    return quantity.quantize(CENTS)


def _require_admin(user) -> None:
    if not (getattr(user, "can_use_system", False) and user.is_warehouse_admin):
        logger.warning(
            "Security: unauthorized admin operation attempted by user '%s' (id: %s)",
            getattr(user, "username", "anonymous"),
            getattr(user, "pk", None),
        )
        raise PermissionDenied(_("Bu amal faqat administratorlar uchun."))


def _require_access(user, material: BaseMaterial) -> None:
    if not user_can_access_material(user, material):
        logger.warning(
            "Security: material access denied for user '%s' (roles: %s) to material '%s' (id: %s, category: %s)",
            getattr(user, "username", "anonymous"),
            getattr(user, "worker_roles", []),
            material.name,
            material.pk,
            material.category,
        )
        raise PermissionDenied(_("Bu material sizning kasbingiz uchun ruxsat etilmagan."))


def _require_taker(user) -> None:
    """Taking stock off the shelf is a worker's action, never an administrator's.

    The administrator books arrivals, sets minimums and reads the history; the
    ledger should name the person who actually carried the material away. Checked
    here rather than only in the templates, so the REST API is bound by it too.
    """
    if getattr(user, "is_warehouse_admin", False):
        logger.warning(
            "Security: warehouse administrator '%s' (id: %s) attempted stock withdrawal",
            getattr(user, "username", "anonymous"),
            getattr(user, "pk", None),
        )
        raise PermissionDenied(_("Administrator ombordan material olmaydi — buni ishchi bajaradi."))


def _locked_stock(material: BaseMaterial) -> WholeMaterial:
    """Fetch (creating if missing) and row-lock the WholeMaterial for ``material``."""
    WholeMaterial.objects.get_or_create(material=material)
    return WholeMaterial.objects.select_for_update().get(material_id=material.pk)


# ---------------------------------------------------------------------------
# Material catalogue
# ---------------------------------------------------------------------------
@transaction.atomic
def create_material(
    *,
    name: str,
    category: str,
    unit_of_measure: str,
    allowed_roles: list[str] | None = None,
    minimum_threshold: Decimal = Decimal("0.00"),
    length_mm: int | None = None,
    width_mm: int | None = None,
) -> BaseMaterial:
    material = BaseMaterial(
        name=name.strip(),
        category=category,
        unit_of_measure=unit_of_measure,
        allowed_roles=list(allowed_roles) if allowed_roles else default_roles_for_category(category),
        minimum_threshold=minimum_threshold or Decimal("0.00"),
    )
    material.full_clean()
    material.save()
    if not material.supports_offcuts:
        length_mm = width_mm = None
    stock = WholeMaterial(material=material, length_mm=length_mm, width_mm=width_mm)
    stock.full_clean()
    stock.save()
    return material


@transaction.atomic
def update_material(material: BaseMaterial, *, changes: dict, dimensions: dict) -> BaseMaterial:
    stock = _locked_stock(material)
    current = BaseMaterial.objects.select_for_update().get(pk=material.pk)
    if changes.get("unit_of_measure", current.unit_of_measure) != current.unit_of_measure:
        if stock.quantity or current.transactions.exists() or current.offcuts.exists():
            raise ValidationError({"unit_of_measure": _("Zaxirasi yoki tarixi bor materialning birligini almashtirib bo'lmaydi.")})
    if current.is_active and changes.get("is_active") is False and (stock.quantity or current.offcuts.exists()):
        raise ValidationError({"is_active": _("Omborda zaxirasi yoki qoldig'i bor materialni arxivlab bo'lmaydi.")})
    if changes.get("category", current.category) not in offcut_category_codes() and current.offcuts.exists():
        raise ValidationError({"category": _("Qoldig'i bor materialni o'lchamsiz kategoriyaga o'tkazib bo'lmaydi.")})
    for field, value in changes.items():
        setattr(current, field, value)
    if not current.allowed_roles:
        current.allowed_roles = default_roles_for_category(current.category)
    current.full_clean()
    current.save(update_fields=[*changes, "allowed_roles", "updated_at"])
    if not current.supports_offcuts:
        dimensions = {"length_mm": None, "width_mm": None}
    if dimensions:
        for field, value in dimensions.items():
            setattr(stock, field, value)
        stock.full_clean()
        stock.save(update_fields=[*dimensions, "updated_at"])
    return current


# ---------------------------------------------------------------------------
# Kirim: stock arrival (admin only)
# ---------------------------------------------------------------------------
@transaction.atomic
def record_kirim(
    *,
    material: BaseMaterial,
    user,
    quantity,
    unit_price_usd=None,
    total_price_usd=None,
    notes: str = "",
    idempotency_key=None,
) -> InventoryTransaction:
    """
    Book a purchase. Pass *either* the per-unit price or the total batch price (USD).

    The material's ``unit_price_usd`` becomes the weighted average cost of the
    stock on hand plus this batch, so the valuation stays accurate when purchase
    prices change between batches.
    """
    _require_admin(user)
    storage_key = None
    if idempotency_key:
        # Serialize retries by the same actor even when their payload targets a different material.
        get_user_model().objects.select_for_update().get(pk=user.pk)
        storage_key = sha256(f"{user.pk}:{idempotency_key}".encode()).hexdigest()
        existing = InventoryTransaction.objects.filter(idempotency_key=storage_key).first()
        if existing is not None:
            if existing.material_id != material.pk or existing.quantity_changed != _decimal(quantity, "quantity") or (
                total_price_usd is not None and existing.total_price_usd != _decimal(total_price_usd, "total_price_usd")
            ) or (
                unit_price_usd is not None and existing.unit_price_usd != _decimal(unit_price_usd, "unit_price_usd")
            ):
                raise ValidationError(_("Bu kirim so'rovi boshqa ma'lumot bilan yuborilgan. Sahifani yangilang."))
            return existing
    stock = _locked_stock(material)
    base = BaseMaterial.objects.select_for_update().get(pk=material.pk)
    quantity = _positive_quantity(base, quantity)

    if (unit_price_usd is None) == (total_price_usd is None):
        raise ValidationError(_("Birlik narxi yoki umumiy partiya narxidan bittasini kiriting."))
    if total_price_usd is not None:
        batch_total = _decimal(total_price_usd, "total_price_usd")
        if batch_total < 0:
            raise ValidationError({"total_price_usd": _("Narx manfiy bo'lishi mumkin emas.")})
        unit_price = (batch_total / quantity).quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)
        if batch_total > 0 and unit_price == ZERO:
            raise ValidationError(
                {"total_price_usd": _("Miqdorga nisbatan narx juda past — birlik narxi nolga tushadi.")}
            )
    else:
        unit_price = _decimal(unit_price_usd, "unit_price_usd")
        if unit_price < 0:
            raise ValidationError({"unit_price_usd": _("Narx manfiy bo'lishi mumkin emas.")})
        unit_price = unit_price.quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)
        batch_total = unit_price * quantity
    batch_total = batch_total.quantize(CENTS, rounding=ROUND_HALF_UP)
    if unit_price > MAX_UNIT_PRICE:
        raise ValidationError({"price_usd": _("Birlik narxi ruxsat etilgan chegaradan oshadi.")})
    if batch_total > MAX_TOTAL_PRICE:
        raise ValidationError({"total_price_usd": _("Partiya narxi juda katta (max %(max)s USD).") % {"max": MAX_TOTAL_PRICE}})

    if not base.is_active:
        raise ValidationError(_("Arxivlangan materialga kirim qilib bo'lmaydi. Avval uni faollashtiring."))

    old_quantity = stock.quantity
    new_quantity = old_quantity + quantity
    if new_quantity > MAX_QUANTITY:
        raise ValidationError({"quantity": _("Jami zaxira hajmi juda katta (max %(max)s).") % {"max": MAX_QUANTITY}})
    if old_quantity <= 0:
        average_price = unit_price
    else:
        old_value = old_quantity * base.unit_price_usd
        average_price = ((old_value + batch_total) / new_quantity).quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)

    previous_price = base.unit_price_usd
    base.unit_price_usd = average_price
    base.save(update_fields=["unit_price_usd", "updated_at"])
    stock.quantity = new_quantity
    stock.save(update_fields=["quantity", "updated_at"])

    entry = InventoryTransaction.objects.create(
        transaction_type=TransactionType.KIRIM,
        material=base,
        user=user,
        quantity_changed=quantity,
        unit_price_usd=unit_price,
        total_price_usd=batch_total,
        stock_after=new_quantity,
        unit_of_measure=base.unit_of_measure,
        notes=notes.strip(),
        idempotency_key=storage_key,
        stock_unit_price_before=previous_price,
    )
    material.unit_price_usd = average_price
    return entry


# ---------------------------------------------------------------------------
# Chiqim: worker withdrawals
# ---------------------------------------------------------------------------
def withdraw_whole_material(*, material: BaseMaterial, user, quantity, notes: str = "") -> InventoryTransaction:
    """Worker takes whole units (sheets, litres, pieces...) from the shelf."""
    _require_taker(user)
    _require_access(user, material)
    with transaction.atomic():
        stock = _locked_stock(material)
        base = BaseMaterial.objects.get(pk=material.pk)
        quantity = _positive_quantity(base, quantity)
        if not base.is_active:
            raise ValidationError(_("Bu material arxivlangan."))
        if quantity > stock.quantity:
            raise InsufficientStockError(
                {"quantity": _("Omborda faqat %(amount)s %(unit)s bor.") % {
                    "amount": f"{stock.quantity.normalize():f}", "unit": base.unit_short}},
                code="insufficient_stock",
            )
        stock.quantity -= quantity
        stock.save(update_fields=["quantity", "updated_at"])

        unit_price = base.unit_price_usd
        return InventoryTransaction.objects.create(
            transaction_type=TransactionType.CHIQIM,
            material=base,
            user=user,
            quantity_changed=-quantity,
            unit_price_usd=unit_price,
            total_price_usd=(unit_price * quantity).quantize(CENTS, rounding=ROUND_HALF_UP),
            stock_after=stock.quantity,
            unit_of_measure=base.unit_of_measure,
            notes=notes.strip(),
        )


def withdraw_offcut(*, offcut: LeftoverOffcut, user, quantity: int = 1, notes: str = "") -> InventoryTransaction:
    """Worker reuses offcut piece(s). The record is removed once no pieces remain."""
    _require_taker(user)
    _require_access(user, offcut.material)
    try:
        pieces = int(quantity)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"quantity": _("Butun son kiriting.")}) from exc
    if pieces < 1 or Decimal(str(quantity)) != pieces:
        raise ValidationError({"quantity": _("Kamida 1 dona (butun son) kiriting.")})

    with transaction.atomic():
        base = BaseMaterial.objects.select_for_update().get(pk=offcut.material_id)
        _require_access(user, base)
        locked = LeftoverOffcut.objects.select_for_update().filter(pk=offcut.pk).first()
        if locked is None:
            raise InsufficientStockError(_("Bu qoldiq allaqachon olingan."), code="offcut_gone")
        if pieces > locked.quantity:
            raise InsufficientStockError(
                {"quantity": _("Bu qoldiqdan faqat %(count)s dona bor.") % {"count": locked.quantity}},
                code="insufficient_stock",
            )
        length_mm, width_mm, material_id = locked.length_mm, locked.width_mm, locked.material_id
        remaining = locked.quantity - pieces
        if remaining == 0:
            locked.delete()
        else:
            locked.quantity = remaining
            locked.save(update_fields=["quantity"])

        return InventoryTransaction.objects.create(
            transaction_type=TransactionType.CHIQIM,
            material_id=material_id,
            user=user,
            quantity_changed=Decimal(-pieces),
            unit_price_usd=ZERO,
            total_price_usd=ZERO,
            unit_of_measure="PIECE",
            is_offcut=True,
            offcut_length_mm=length_mm,
            offcut_width_mm=width_mm,
            offcut_record_id=offcut.pk,
            notes=notes.strip(),
        )


# ---------------------------------------------------------------------------
# Qoldiq: offcut registration
# ---------------------------------------------------------------------------
@transaction.atomic
def register_offcut(
    *,
    material: BaseMaterial,
    user,
    length_mm: int,
    width_mm: int,
    quantity: int = 1,
    notes: str = "",
) -> LeftoverOffcut:
    """Log a reusable leftover piece after cutting a sheet."""
    material = BaseMaterial.objects.select_for_update().get(pk=material.pk)
    _require_access(user, material)
    if not material.is_active:
        raise ValidationError(_("Bu material arxivlangan."))
    if not material.supports_offcuts:
        raise ValidationError({"material": _("Bu turdagi material uchun qoldiq qayd etilmaydi.")})

    errors = {}
    for field, value in (("length_mm", length_mm), ("width_mm", width_mm), ("quantity", quantity)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            errors[field] = _("Musbat butun son kiriting.")
    if errors:
        raise ValidationError(errors)

    stock = WholeMaterial.objects.filter(material=material).first()
    if stock and stock.length_mm and stock.width_mm:
        piece = sorted((length_mm, width_mm))
        sheet = sorted((stock.length_mm, stock.width_mm))
        if piece[0] > sheet[0] or piece[1] > sheet[1]:
            raise ValidationError(
                _("Qoldiq o'lchami to'liq listdan (%(length)sx%(width)s mm) katta bo'lishi mumkin emas.")
                % {"length": stock.length_mm, "width": stock.width_mm}
            )

    with transaction.atomic():
        offcut = LeftoverOffcut.objects.create(
            material=material,
            length_mm=length_mm,
            width_mm=width_mm,
            quantity=quantity,
            notes=notes.strip(),
            created_by=user,
        )
        InventoryTransaction.objects.create(
            transaction_type=TransactionType.OFFCUT_DEPOSIT,
            material=material,
            user=user,
            quantity_changed=Decimal(quantity),
            unit_price_usd=ZERO,
            total_price_usd=ZERO,
            unit_of_measure="PIECE",
            is_offcut=True,
            offcut_length_mm=length_mm,
            offcut_width_mm=width_mm,
            offcut_record_id=offcut.pk,
            notes=notes.strip(),
        )
    return offcut


# ---------------------------------------------------------------------------
# Reversal: admin corrects an incorrect entry
# ---------------------------------------------------------------------------
@transaction.atomic
def reverse_transaction(*, entry: InventoryTransaction, user, reason: str = "") -> InventoryTransaction:
    """Append a correction on the day it is made and adjust the current stock."""
    _require_admin(user)
    if entry.transaction_type == TransactionType.REVERSAL:
        raise ValidationError(_("Qaytarish yozuvini qaytarib bo'lmaydi."))

    stock = None if entry.is_offcut else _locked_stock(entry.material)
    base = BaseMaterial.objects.select_for_update().get(pk=entry.material_id)
    if InventoryTransaction.objects.filter(reversed_entry=entry).exists():
        raise ValidationError(_("Bu yozuv allaqachon qaytarilgan."))
    if not base.is_active:
        raise ValidationError(_("Avval materialni faollashtiring."))

    stock_after = None
    offcut_record_id = entry.offcut_record_id
    if entry.is_offcut:
        offcut_record_id = _reverse_offcut(entry, user)
    else:
        stock_after = _reverse_whole(entry, base, stock)

    return InventoryTransaction.objects.create(
        transaction_type=TransactionType.REVERSAL,
        material=base,
        user=user,
        quantity_changed=-entry.quantity_changed,
        unit_price_usd=entry.unit_price_usd,
        total_price_usd=entry.total_price_usd,
        stock_after=stock_after,
        unit_of_measure=entry.unit_of_measure or ("PIECE" if entry.is_offcut else base.unit_of_measure),
        is_offcut=entry.is_offcut,
        offcut_length_mm=entry.offcut_length_mm,
        offcut_width_mm=entry.offcut_width_mm,
        offcut_record_id=offcut_record_id,
        reversed_entry=entry,
        notes=reason.strip(),
    )


def _reverse_whole(entry: InventoryTransaction, base: BaseMaterial, stock: WholeMaterial) -> Decimal:
    if entry.unit_of_measure and entry.unit_of_measure != base.unit_of_measure:
        raise ValidationError(_("Bu harakatning birligi hozirgi material birligidan farq qiladi."))
    new_quantity = stock.quantity - entry.quantity_changed
    if not ZERO <= new_quantity <= MAX_QUANTITY:
        raise ValidationError(_("Qaytarish uchun ombordagi miqdor yetarli emas yoki chegaradan oshadi."))

    if entry.transaction_type == TransactionType.KIRIM:
        newer = InventoryTransaction.objects.filter(material=base, is_offcut=False).filter(
            Q(created_at__gt=entry.created_at) | Q(created_at=entry.created_at, pk__gt=entry.pk)
        )
        # A later active movement may have used this batch's cost. Undo it first.
        if newer.exclude(
            Q(reversal__isnull=False) | Q(transaction_type=TransactionType.REVERSAL)
        ).exists():
            raise ValidationError(_("Avval bu kirimdan keyingi butun material harakatlarini qaytaring."))
        if entry.stock_unit_price_before is not None and entry.stock_after == stock.quantity:
            new_price = entry.stock_unit_price_before if new_quantity else ZERO
        else:
            remaining_value = stock.quantity * base.unit_price_usd - entry.total_price_usd
            # Legacy rates were rounded to four places. Tiny negative differences carry no value.
            if remaining_value < -(stock.quantity * PRICE_PLACES / 2 + CENTS / 2) and new_quantity:
                raise ValidationError(_("Qaytarishdan keyin zaxira qiymatini ishonchli hisoblab bo'lmadi."))
            new_price = max(ZERO, remaining_value) / new_quantity if new_quantity else ZERO
    else:
        returned_value = -entry.quantity_changed * entry.unit_price_usd
        new_price = (stock.quantity * base.unit_price_usd + returned_value) / new_quantity
    new_price = new_price.quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)
    if new_price > MAX_UNIT_PRICE:
        raise ValidationError(_("Qaytarishdan keyin birlik narxi ruxsat etilgan chegaradan oshadi."))

    base.unit_price_usd = new_price
    base.save(update_fields=["unit_price_usd", "updated_at"])
    stock.quantity = new_quantity
    stock.save(update_fields=["quantity", "updated_at"])
    return new_quantity


def _reverse_offcut(entry: InventoryTransaction, user) -> int:
    pieces = int(abs(entry.quantity_changed))
    offcut = LeftoverOffcut.objects.select_for_update().filter(
        pk=entry.offcut_record_id, material_id=entry.material_id,
    ).first()
    if entry.transaction_type == TransactionType.OFFCUT_DEPOSIT:
        if entry.offcut_record_id is None:
            # Older deposits had no reference. Use only a unique, still-intact match.
            similar = InventoryTransaction.objects.filter(
                material_id=entry.material_id, transaction_type=TransactionType.OFFCUT_DEPOSIT,
                user_id=entry.user_id,
                offcut_length_mm=entry.offcut_length_mm, offcut_width_mm=entry.offcut_width_mm,
                notes=entry.notes,
            )
            if similar.count() == 1:
                candidates = list(LeftoverOffcut.objects.select_for_update().filter(
                    material_id=entry.material_id, created_by_id=entry.user_id,
                    length_mm=entry.offcut_length_mm, width_mm=entry.offcut_width_mm,
                    quantity=pieces, notes=entry.notes, created_at__lte=entry.created_at,
                )[:2])
                if len(candidates) == 1:
                    offcut = candidates[0]
        if offcut is None or offcut.quantity < pieces:
            raise ValidationError(_("Qoldiqning asl yozuvi topilmadi yoki undan foydalanilgan."))
        offcut_id = offcut.pk
        if offcut.quantity == pieces:
            offcut.delete()
        else:
            offcut.quantity -= pieces
            offcut.save(update_fields=["quantity"])
        return offcut_id

    if offcut is None:
        origin = InventoryTransaction.objects.filter(
            material_id=entry.material_id, offcut_record_id=entry.offcut_record_id,
            transaction_type=TransactionType.OFFCUT_DEPOSIT,
        ).first() if entry.offcut_record_id else None
        offcut = LeftoverOffcut.objects.create(
            pk=entry.offcut_record_id,
            material_id=entry.material_id,
            length_mm=entry.offcut_length_mm,
            width_mm=entry.offcut_width_mm,
            quantity=pieces,
            created_by=origin.user if origin else user,
            notes=origin.notes if origin else "",
        )
    else:
        offcut.quantity += pieces
        offcut.save(update_fields=["quantity"])
    return offcut.pk


# ---------------------------------------------------------------------------
# Operating expenses (admin only): nalog, produkta, stanok
# ---------------------------------------------------------------------------
def record_expense(
    *,
    user,
    category: str,
    title: str,
    amount_usd,
    expense_date=None,
    notes: str = "",
) -> OperatingExpense:
    """Record an operating expense (nalog, produkta, stanok, or other)."""
    _require_admin(user)
    if category not in ExpenseCategory.values:
        raise ValidationError({"category": _("Noma'lum xarajat turi.")})
    clean_title = " ".join((title or "").split())
    if not clean_title:
        raise ValidationError({"title": _("Xarajat nomini kiriting.")})
    amount = _decimal(amount_usd, "amount_usd")
    if amount <= 0:
        raise ValidationError({"amount_usd": _("Summa 0 dan katta bo'lishi kerak.")})
    if amount != amount.quantize(CENTS):
        raise ValidationError({"amount_usd": _("Summa ko'pi bilan 2 xonali kasr bo'lishi mumkin.")})
    amount = amount.quantize(CENTS, rounding=ROUND_HALF_UP)

    return OperatingExpense.objects.create(
        category=category,
        title=clean_title,
        amount_usd=amount,
        expense_date=expense_date or timezone.localdate(),
        notes=(notes or "").strip(),
        created_by=user,
    )
