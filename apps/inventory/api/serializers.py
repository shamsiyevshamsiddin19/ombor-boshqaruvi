from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.users.models import worker_role_choices

from .. import services
from ..models import (
    BaseMaterial,
    ExpenseCategory,
    InventoryTransaction,
    LeftoverOffcut,
    OperatingExpense,
    WholeMaterial,
)

PRICE_FIELDS = ("unit_price_usd", "total_price_usd", "total_value_usd")


class HidePricesForWorkersMixin:
    """Workers see quantities; purchase prices and valuations are admin-only."""

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        if request is not None and not request.user.is_warehouse_admin:
            for key in PRICE_FIELDS:
                data.pop(key, None)
        return data


class BaseMaterialSerializer(serializers.ModelSerializer):
    allowed_roles = serializers.MultipleChoiceField(choices=(), required=False)
    unit_short = serializers.CharField(read_only=True)
    quantity = serializers.DecimalField(
        source="whole_stock.quantity", max_digits=12, decimal_places=2, read_only=True
    )
    length_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    width_mm = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allowed_roles"].choices = worker_role_choices()  # trades live in the database

    class Meta:
        model = BaseMaterial
        fields = (
            "id",
            "name",
            "category",
            "allowed_roles",
            "unit_of_measure",
            "unit_short",
            "minimum_threshold",
            "unit_price_usd",
            "quantity",
            "length_mm",
            "width_mm",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("unit_price_usd", "created_at", "updated_at")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        stock = getattr(instance, "whole_stock", None)
        data["allowed_roles"] = list(instance.allowed_roles)
        data["length_mm"] = stock.length_mm if stock else None
        data["width_mm"] = stock.width_mm if stock else None
        return data

    def validate(self, attrs):
        length = attrs.get("length_mm", getattr(getattr(self.instance, "whole_stock", None), "length_mm", None))
        width = attrs.get("width_mm", getattr(getattr(self.instance, "whole_stock", None), "width_mm", None))
        if bool(length) != bool(width):
            raise serializers.ValidationError({"width_mm": _("O'lchamlar (uzunlik va kenglik) birga berilishi kerak.")})
        return attrs

    def create(self, validated_data):
        validated_data.pop("is_active", None)
        roles = validated_data.pop("allowed_roles", None)
        return services.create_material(allowed_roles=list(roles) if roles else None, **validated_data)

    def update(self, instance, validated_data):
        dims = {key: validated_data.pop(key) for key in ("length_mm", "width_mm") if key in validated_data}
        if "allowed_roles" in validated_data:
            validated_data["allowed_roles"] = list(validated_data["allowed_roles"])
        try:
            return services.update_material(instance, changes=validated_data, dimensions=dims)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc


class MaterialSummarySerializer(serializers.ModelSerializer):
    unit_short = serializers.CharField(read_only=True)

    class Meta:
        model = BaseMaterial
        fields = ("id", "name", "category", "unit_of_measure", "unit_short", "allowed_roles")


class WholeMaterialSerializer(HidePricesForWorkersMixin, serializers.ModelSerializer):
    material = MaterialSummarySerializer(read_only=True)
    unit_price_usd = serializers.DecimalField(
        source="material.unit_price_usd", max_digits=12, decimal_places=4, read_only=True
    )
    minimum_threshold = serializers.DecimalField(
        source="material.minimum_threshold", max_digits=12, decimal_places=2, read_only=True
    )
    total_value_usd = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    is_below_threshold = serializers.BooleanField(read_only=True)

    class Meta:
        model = WholeMaterial
        fields = (
            "id",
            "material",
            "quantity",
            "length_mm",
            "width_mm",
            "unit_price_usd",
            "minimum_threshold",
            "total_value_usd",
            "is_below_threshold",
            "updated_at",
        )
        read_only_fields = fields


class LeftoverOffcutSerializer(serializers.ModelSerializer):
    material = MaterialSummarySerializer(read_only=True)
    material_id = serializers.PrimaryKeyRelatedField(
        source="material", queryset=BaseMaterial.objects.all(), write_only=True
    )
    created_by = serializers.StringRelatedField(read_only=True)
    length_mm = serializers.IntegerField(min_value=1, max_value=100_000)
    width_mm = serializers.IntegerField(min_value=1, max_value=100_000)
    quantity = serializers.IntegerField(min_value=1, max_value=1000, default=1)

    class Meta:
        model = LeftoverOffcut
        fields = ("id", "material", "material_id", "length_mm", "width_mm", "quantity", "notes", "created_by", "created_at")
        read_only_fields = ("id", "created_by", "created_at")


class TransactionSerializer(HidePricesForWorkersMixin, serializers.ModelSerializer):
    material = MaterialSummarySerializer(read_only=True)
    user = serializers.StringRelatedField(read_only=True)
    unit_of_measure = serializers.CharField(source="movement_unit", read_only=True)

    class Meta:
        model = InventoryTransaction
        fields = (
            "id",
            "transaction_type",
            "material",
            "user",
            "quantity_changed",
            "unit_of_measure",
            "unit_price_usd",
            "total_price_usd",
            "stock_after",
            "is_offcut",
            "offcut_length_mm",
            "offcut_width_mm",
            "notes",
            "created_at",
        )
        read_only_fields = fields


class WithdrawSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class OffcutWithdrawSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, default=1)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class KirimSerializer(serializers.Serializer):
    material_id = serializers.PrimaryKeyRelatedField(
        source="material", queryset=BaseMaterial.objects.filter(is_active=True)
    )
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    idempotency_key = serializers.UUIDField(required=False)
    unit_price_usd = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal("0"), required=False
    )
    total_price_usd = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000, default="")

    def validate(self, attrs):
        if ("unit_price_usd" in attrs) == ("total_price_usd" in attrs):
            raise serializers.ValidationError("Provide exactly one of unit_price_usd or total_price_usd.")
        return attrs


class OperatingExpenseSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    category = serializers.ChoiceField(choices=ExpenseCategory.choices)
    amount_usd = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = OperatingExpense
        fields = ("id", "category", "title", "amount_usd", "expense_date", "notes", "created_by", "created_at")
        read_only_fields = ("id", "created_by", "created_at")
