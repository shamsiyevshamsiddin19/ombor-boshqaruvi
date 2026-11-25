import datetime as dt
from decimal import Decimal

from django import forms
from django.core.validators import MaxValueValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.users.forms import unique_code
from apps.users.models import User, worker_role_choices

from .models import (
    BaseMaterial,
    Category,
    ExpenseCategory,
    MaterialCategory,
    MeasurementUnit,
    Project,
    WholeMaterial,
    active_category_choices,
    category_choices,
    default_roles_for_category,
    expense_category_choices,
    localized_category_name,
    transaction_type_choices,
)
from .selectors import offcut_capable_materials

MAX_MM = 100_000

#: Value of the "Boshqa" option in the category dropdown; the admin then types a new category name.
OTHER_CATEGORY = "__other__"


def material_category_choices(include_code: str = "") -> list[tuple[str, str]]:
    return [("", _("Tanlang")), *active_category_choices(include_code=include_code), (OTHER_CATEGORY, _("Boshqa"))]


def report_category_choices() -> list[tuple[str, str]]:
    used_codes = set(BaseMaterial.objects.values_list("category", flat=True).distinct())
    seen = []
    for code, label in active_category_choices():
        seen.append((code, label))
    active_set = {code for code, _ in seen}
    for code, label in category_choices():
        if code in used_codes and code not in active_set:
            seen.append((code, label))
    return [("", _("Barchasi")), *seen]


def category_code_from(name: str) -> str:
    return unique_code(name, lambda code: Category.objects.filter(code=code).exists(), "KATEGORIYA", 50)


class MaterialChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: BaseMaterial) -> str:
        return f"{obj.name} ({obj.unit_short})"


class MaterialForm(forms.ModelForm):
    """Admin: create or edit a material card (and its standard sheet size)."""

    category = forms.ChoiceField(label=_("Kategoriya"), choices=material_category_choices)
    new_category = forms.CharField(
        label=_("Yangi kategoriya nomi"), required=False, max_length=60,
        widget=forms.TextInput(attrs={"placeholder": _("Masalan: Oyna va ko'zgu")}),
    )
    new_category_supports_offcuts = forms.BooleanField(
        label=_("O'lcham (list) va qoldiq yuritiladi"),
        required=False,
        help_text=_("Belgilansa, list o'lchami (uzunlik × eni) va kesilgan qoldiqlar qo'shiladi."),
    )
    allowed_roles = forms.MultipleChoiceField(
        label=_("Qaysi kasblar ko'ra oladi va oladi"),
        choices=worker_role_choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text=_("Bo'sh qoldirilsa, kategoriya bo'yicha standart kasblar belgilanadi."),
    )
    dim_unit = forms.ChoiceField(
        label=_("O'lcham birligi"),
        choices=[("mm", "mm"), ("sm", "sm")],
        required=False,
        initial="mm",
    )
    length_mm = forms.IntegerField(
        label=_("List uzunligi"), required=False, min_value=1, max_value=MAX_MM,
        help_text=_("Faqat o'lchamli materiallar uchun, masalan 2750 mm (yoki 275 sm)."),
    )
    width_mm = forms.IntegerField(
        label=_("List eni"), required=False, min_value=1, max_value=MAX_MM,
        help_text=_("Masalan 1830 mm (yoki 183 sm)."),
    )
    # Whole numbers only in the form; the model columns stay decimal so existing data is untouched.
    minimum_threshold = forms.IntegerField(
        label=_("Minimal qoldiq"), min_value=0, max_value=10**9, initial=0,
        help_text=_("Miqdor shundan kam bo'lsa, xarid ro'yxatida ko'rinadi."),
        widget=forms.NumberInput(attrs={"step": "1", "inputmode": "numeric"}),
    )
    container_size = forms.IntegerField(
        label=_("Idish hajmi"), required=False, min_value=1, max_value=10**7,
        help_text=_("Masalan, 12 litrli chelak uchun 12. Kirimda idish soni bilan hisoblash uchun."),
        widget=forms.NumberInput(attrs={"step": "1", "inputmode": "numeric"}),
    )

    class Meta:
        model = BaseMaterial
        fields = (
            "name",
            "category",
            "unit_of_measure",
            "container_size",
            "minimum_threshold",
            "allowed_roles",
            "is_active",
        )
        help_texts = {
            "is_active": _("O'chirilsa, ishchilar materialni ko'rmaydi; tarix saqlanadi."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_category_name = ""  # set by clean() when "Boshqa" names a category that doesn't exist yet
        if self.instance.pk:
            self.fields["category"].choices = material_category_choices(include_code=self.instance.category)
        stock = getattr(self.instance, "whole_stock", None) if self.instance.pk else None
        if stock is not None:
            self.fields["length_mm"].initial = stock.length_mm
            self.fields["width_mm"].initial = stock.width_mm
        self.fields["unit_of_measure"].choices = [("", _("Tanlang")), *MeasurementUnit.choices]
        # Show stored whole values without the ".00" the decimal columns carry.
        for name in ("minimum_threshold", "container_size"):
            value = getattr(self.instance, name, None) if self.instance.pk else None
            if value is not None and value == value.to_integral_value():
                self.initial[name] = int(value)
        if not self.instance.pk:
            self.fields.pop("is_active")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("category") == OTHER_CATEGORY:
            self._resolve_new_category(cleaned)
        category = cleaned.get("category")
        if category and category != OTHER_CATEGORY and not cleaned.get("allowed_roles") and not default_roles_for_category(category):
            self.add_error("allowed_roles", _("Bu kategoriya uchun kamida bitta kasbni belgilang."))
        length, width = cleaned.get("length_mm"), cleaned.get("width_mm")
        if bool(length) != bool(width):
            self.add_error("width_mm" if length else "length_mm", _("Uzunlik va enni birga kiriting."))
        elif length and width and cleaned.get("dim_unit") == "sm":
            if length * 10 > MAX_MM:
                self.add_error("length_mm", _("Uzunlik %(max)s sm dan oshmasligi kerak.") % {"max": MAX_MM // 10})
            if width * 10 > MAX_MM:
                self.add_error("width_mm", _("Eni %(max)s sm dan oshmasligi kerak.") % {"max": MAX_MM // 10})
            if not self.has_error("length_mm") and not self.has_error("width_mm"):
                cleaned["length_mm"] = length * 10
                cleaned["width_mm"] = width * 10
        if category in (MaterialCategory.LIQUID, "KRASKA", "GRUNTOFKA") and cleaned.get("unit_of_measure") in (
            MeasurementUnit.METER,
            MeasurementUnit.CENTIMETER,
        ):
            self.add_error("unit_of_measure", _("Suyuqliklar uchun litr, kg yoki dona tanlang."))
        return cleaned

    def _resolve_new_category(self, cleaned) -> None:
        name = " ".join((cleaned.get("new_category") or "").split())
        if not name:
            self.add_error("new_category", _("Yangi kategoriya nomini yozing."))
            return
        # A name that already exists (as stored or as translated, e.g. "Фурнитура") just selects that category.
        wanted = name.casefold()
        existing = next(
            (code for code, stored in Category.objects.values_list("code", "name")
             if wanted in (stored.casefold(), localized_category_name(stored).casefold())),
            None,
        )
        if existing is not None:
            cleaned["category"] = existing
        else:
            self.pending_category_name = name
            cleaned["category"] = category_code_from(name)

    def _get_validation_exclusions(self):
        exclusions = super()._get_validation_exclusions()
        if self.pending_category_name:
            exclusions.add("category")  # the new category row is only created once the whole form is valid
        return exclusions

    def create_pending_category(self) -> None:
        """Create the category typed under "Boshqa". Call inside the transaction that saves the material."""
        if not self.pending_category_name:
            return
        last = Category.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
        Category.objects.create(
            code=self.cleaned_data["category"],
            name=self.pending_category_name,
            sort_order=last + 10,
            supports_offcuts=bool(self.cleaned_data.get("new_category_supports_offcuts")),
            default_roles=list(self.cleaned_data.get("allowed_roles") or []),
            is_active=True,
        )

    def save_stock_dimensions(self, material: BaseMaterial) -> None:
        stock, _ = WholeMaterial.objects.get_or_create(material=material)
        stock.length_mm = self.cleaned_data.get("length_mm")
        stock.width_mm = self.cleaned_data.get("width_mm")
        stock.save(update_fields=["length_mm", "width_mm", "updated_at"])


class KirimForm(forms.Form):
    """Admin: record a stock arrival with either the batch total or the per-unit price."""

    PRICE_TOTAL = "total"
    PRICE_UNIT = "unit"

    category = forms.ChoiceField(
        label=_("Kategoriya"),
        required=False,
        choices=lambda: [("", _("Barcha kategoriyalar")), *active_category_choices()],
    )
    material = MaterialChoiceField(
        queryset=BaseMaterial.objects.filter(is_active=True), label=_("Material"), empty_label=_("Materialni tanlang")
    )
    containers = forms.DecimalField(
        label=_("Idishlar soni"),
        required=False,
        min_value=Decimal("0.01"),
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"x-model.number": "containers", "inputmode": "decimal"}),
        help_text=_("Masalan, 5 chelak. Miqdor idish hajmiga ko'paytirib hisoblanadi."),
    )
    quantity = forms.DecimalField(
        label=_("Miqdor"),
        required=False,
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )
    price_mode = forms.ChoiceField(
        label=_("Narx turi"),
        choices=[(PRICE_TOTAL, _("Optom narx")), (PRICE_UNIT, _("Dona narxi"))],
        widget=forms.RadioSelect(attrs={"x-model": "mode"}),
        initial=PRICE_TOTAL,
    )
    price_usd = forms.DecimalField(
        label=_("Narx (USD $)"), min_value=Decimal("0"), max_digits=14, decimal_places=4
    )
    notes = forms.CharField(
        label=_("Izoh"),
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": _("Yetkazib beruvchi, faktura raqami...")}),
    )

    def clean(self):
        cleaned = super().clean()
        material: BaseMaterial | None = cleaned.get("material")
        containers = cleaned.get("containers")
        quantity = cleaned.get("quantity")
        if material and containers:
            if not material.container_size:
                self.add_error("containers", _("Bu material uchun idish hajmi belgilanmagan."))
            else:
                quantity = (containers * material.container_size).quantize(Decimal("0.01"))
                cleaned["quantity"] = quantity
        if not quantity and not self.has_error("containers") and not self.has_error("quantity"):
            self.add_error("quantity", _("Miqdorni yoki idishlar sonini kiriting."))
        price = cleaned.get("price_usd")
        if cleaned.get("price_mode") == self.PRICE_TOTAL and price is not None and price != price.quantize(Decimal("0.01")):
            self.add_error("price_usd", _("Optom narx ko'pi bilan 2 xonali kasr bo'lishi mumkin."))
        return cleaned

    def service_kwargs(self) -> dict:
        data = self.cleaned_data
        price_key = "total_price_usd" if data["price_mode"] == self.PRICE_TOTAL else "unit_price_usd"
        return {
            "material": data["material"],
            "quantity": data["quantity"],
            price_key: data["price_usd"],
            "notes": data.get("notes", ""),
        }


class WithdrawWholeForm(forms.Form):
    quantity = forms.DecimalField(
        label=_("Qancha oldingiz?"), min_value=Decimal("0.01"), max_digits=12, decimal_places=2
    )
    project = forms.ModelChoiceField(
        label=_("Loyiha / buyurtma"),
        queryset=Project.objects.filter(is_active=True),
        required=False,
        empty_label=_("— tanlang (ixtiyoriy) —"),
    )
    notes = forms.CharField(
        label=_("Izoh"),
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"placeholder": _("Buyurtma yoki mijoz nomi")}),
    )

    def __init__(self, *args, stock: WholeMaterial, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["quantity"]
        field.widget.attrs.update(
            {"inputmode": "decimal", "autofocus": True, "max": stock.quantity, "x-model.number": "q"}
        )
        if stock.material.requires_integer_quantity:
            field.widget.attrs.update({"step": "1", "inputmode": "numeric"})
            field.initial = 1


class OffcutForm(forms.Form):
    material = MaterialChoiceField(
        queryset=BaseMaterial.objects.none(), label=_("Material"), empty_label=_("Materialni tanlang")
    )
    length_mm = forms.IntegerField(label=_("Uzunligi (mm)"), min_value=1, max_value=MAX_MM)
    width_mm = forms.IntegerField(label=_("Eni (mm)"), min_value=1, max_value=MAX_MM)
    quantity = forms.IntegerField(label=_("Soni (dona)"), min_value=1, max_value=1000, initial=1)
    notes = forms.CharField(
        label=_("Joylashuvi / izoh"),
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={"placeholder": _("Masalan: 2-stellaj, yuqori polka")}),
    )

    def __init__(self, *args, user: User, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["material"].queryset = offcut_capable_materials(user).select_related("whole_stock")
        self.fields["material"].widget.attrs.update({"x-model": "material", "class": "control"})
        for name, model in (("length_mm", "length"), ("width_mm", "width"), ("quantity", "pieces")):
            self.fields[name].widget.attrs.update(
                {"inputmode": "numeric", "x-model.number": model, "class": "control font-mono"}
            )
        self.fields["notes"].widget.attrs.update({"class": "control"})


class WithdrawOffcutForm(forms.Form):
    quantity = forms.IntegerField(label=_("Necha dona oldingiz?"), min_value=1, initial=1)
    project = forms.ModelChoiceField(
        label=_("Loyiha / buyurtma"),
        queryset=Project.objects.filter(is_active=True),
        required=False,
        empty_label=_("— tanlang (ixtiyoriy) —"),
    )
    notes = forms.CharField(label=_("Izoh"), required=False, max_length=500)

    def __init__(self, *args, max_quantity: int, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["quantity"]
        field.max_value = max_quantity
        field.validators.append(MaxValueValidator(max_quantity))
        field.widget.attrs.update({"max": max_quantity, "inputmode": "numeric", "x-model.number": "q"})


class ReportFilterForm(forms.Form):
    month = forms.CharField(
        label=_("Oy"), required=False, widget=forms.TextInput(attrs={"type": "month"})
    )
    transaction_type = forms.ChoiceField(
        label=_("Harakat turi"), required=False, choices=lambda: [("", _("Barchasi")), *transaction_type_choices()]
    )
    category = forms.ChoiceField(
        label=_("Kategoriya"), required=False, choices=report_category_choices
    )
    user = forms.ModelChoiceField(
        label=_("Ishchi"), required=False, queryset=User.objects.order_by("full_name", "username"),
        empty_label=_("Barchasi"),
    )
    material = MaterialChoiceField(
        label=_("Material"), required=False, queryset=BaseMaterial.objects.order_by("name"), empty_label=_("Barchasi")
    )
    project = forms.ModelChoiceField(
        label=_("Loyiha"), required=False,
        queryset=Project.objects.all().order_by("-is_active", "name"),
        empty_label=_("Barchasi"),
    )

    def clean_month(self) -> dt.date | None:
        value = self.cleaned_data.get("month")
        if not value:
            return None
        try:
            return dt.datetime.strptime(value, "%Y-%m").date()
        except ValueError as exc:
            raise forms.ValidationError(_("Oyni YYYY-MM ko'rinishida kiriting.")) from exc

    def selector_kwargs(self) -> dict:
        if not self.is_bound:
            return {}
        self.is_valid()
        data = self.cleaned_data
        return {
            "month": data.get("month"),
            "category": data.get("category", ""),
            "transaction_type": data.get("transaction_type", ""),
            "user_id": data["user"].pk if data.get("user") else None,
            "material_id": data["material"].pk if data.get("material") else None,
            "project_id": data["project"].pk if data.get("project") else None,
        }


class ExpenseForm(forms.Form):
    """Admin: record operating expenses such as nalog, produkta, stanok."""

    category = forms.ChoiceField(
        label=_("Xarajat turi"),
        choices=expense_category_choices,
        initial=ExpenseCategory.STANOK,
    )
    title = forms.CharField(
        label=_("Nomi / nima uchun"),
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": _("Masalan: Arra pichog'i, tushlik, soliq...")}),
    )
    amount_usd = forms.DecimalField(
        label=_("Summa (USD $)"),
        min_value=Decimal("0.01"),
        max_digits=14,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0.01", "inputmode": "decimal", "placeholder": "0.00"}),
    )
    expense_date = forms.DateField(
        label=_("Sana"),
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    notes = forms.CharField(
        label=_("Izoh"),
        required=False,
        max_length=1000,
        widget=forms.TextInput(attrs={"placeholder": _("Qo'shimcha izoh...")}),
    )


class ExpenseFilterForm(forms.Form):
    month = forms.CharField(
        label=_("Oy"), required=False, widget=forms.TextInput(attrs={"type": "month"})
    )
    category = forms.ChoiceField(
        label=_("Xarajat turi"),
        required=False,
        choices=lambda: [("", _("Barchasi")), *expense_category_choices()],
    )

    def clean_month(self) -> dt.date | None:
        value = self.cleaned_data.get("month")
        if not value:
            return None
        try:
            return dt.datetime.strptime(value, "%Y-%m").date()
        except ValueError as exc:
            raise forms.ValidationError(_("Oyni YYYY-MM ko'rinishida kiriting.")) from exc

    def selector_kwargs(self) -> dict:
        if not self.is_bound:
            return {}
        self.is_valid()
        data = self.cleaned_data
        return {
            "month": data.get("month"),
            "category": data.get("category", ""),
        }
