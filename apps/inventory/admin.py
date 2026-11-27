from django.contrib import admin

from .models import (
    BaseMaterial,
    Category,
    InventoryTransaction,
    LeftoverOffcut,
    OperatingExpense,
    WholeMaterial,
)


class WholeMaterialInline(admin.StackedInline):
    model = WholeMaterial
    can_delete = False
    readonly_fields = ("quantity",)  # stock only moves through Kirim/Chiqim


@admin.register(BaseMaterial)
class BaseMaterialAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit_of_measure", "unit_price_usd", "minimum_threshold", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("name",)
    readonly_fields = ("unit_price_usd", "created_at", "updated_at")
    inlines = [WholeMaterialInline]

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return (*fields, "category", "unit_of_measure", "is_active") if obj else fields

    def get_inline_instances(self, request, obj=None):
        return super().get_inline_instances(request, obj) if obj and obj.supports_offcuts else []

    def has_delete_permission(self, request, obj=None):
        return False  # archive instead; the ledger references materials


@admin.register(LeftoverOffcut)
class LeftoverOffcutAdmin(admin.ModelAdmin):
    list_display = ("material", "length_mm", "width_mm", "quantity", "created_by", "created_at")
    list_filter = ("material__category",)
    search_fields = ("material__name", "notes")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    """Read-only: the ledger is append-only and written by the service layer."""

    list_display = (
        "created_at",
        "transaction_type",
        "material",
        "quantity_changed",
        "unit_price_usd",
        "total_price_usd",
        "user",
    )
    list_filter = ("transaction_type", "is_offcut", "material__category")
    search_fields = ("material__name", "user__username", "user__full_name", "notes")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "sort_order", "supports_offcuts", "is_active")
    list_filter = ("supports_offcuts", "is_active")
    ordering = ("sort_order", "name")


@admin.register(OperatingExpense)
class OperatingExpenseAdmin(admin.ModelAdmin):
    list_display = ("expense_date", "category", "title", "amount_usd", "created_by", "created_at")
    list_filter = ("category", "expense_date")
    search_fields = ("title", "notes")
    date_hierarchy = "expense_date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
