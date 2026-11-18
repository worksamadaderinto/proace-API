from __future__ import annotations
from typing import Any

from django.db.models import Count
from rest_framework import serializers

from product.models import ProductImg, Product, Specification

from taggit.serializers import TagListSerializerField, TaggitSerializer

from store.models import Store
from utils.functions import normalize_media_url




class StoreInfoForProductCardSerializer(serializers.ModelSerializer):
    class Meta:
        model = Store
        fields = ["url", "name"]


class ProductImgSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImg
        fields = ["id", "product", "image",]

    def to_representation(self, instance: ProductImg) -> dict[str, Any]:
        data = super().to_representation(instance)
        if data.get('image'):
            data['image'] = normalize_media_url(data['image'])
        return data


class SpecificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Specification

        fields = ["product", "serial", "attributes", "height",
                  "width", "breadth", "weight", "color", "created", "updated",]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        product = attrs.get("product") or getattr(self.instance, "product", None)
        if product and (product.is_digital or product.product_type == Product.PRODUCT_TYPE_AFFILIATE_CLICKBANK):
            raise serializers.ValidationError("Digital and affiliate partner products cannot have physical specifications.")
        return attrs


def _batch_product_likes_count(serializer: Any, product: Product) -> int:
    likes_count = getattr(product, "likes_count", None)
    if likes_count is not None:
        return likes_count
    root = getattr(serializer, 'root', serializer)
    context = getattr(root, 'context', None)
    if context is None:
        context = getattr(serializer, 'context', {}) or {}
    counts = context.setdefault('product_likes_counts', {})
    if product.pk in counts:
        return counts[product.pk]
    root_inst = getattr(root, 'instance', None)
    instances = None
    if root_inst:
        if isinstance(root, serializers.ListSerializer) or isinstance(root_inst, (list, tuple)):
            instances = root_inst
        elif hasattr(root_inst, 'cart_items'):
            instances = root_inst.cart_items.all()
    if instances and not counts:
        from core.models import Wishlist
        pks = []
        for inst in instances:
            if hasattr(inst, 'pk') and isinstance(inst, Product):
                pks.append(inst.pk)
            elif hasattr(inst, 'product_id'):
                pks.append(inst.product_id)
            elif hasattr(inst, 'product') and hasattr(inst.product, 'pk'):
                pks.append(inst.product.pk)
        if pks:
            for row in Wishlist.objects.filter(product_id__in=pks, liked=True).values('product_id').annotate(c=Count('pk')):
                counts[row['product_id']] = row['c']
            for pk in pks:
                counts.setdefault(pk, 0)
    if product.pk in counts:
        return counts[product.pk]
    return product.wishlist_set.count()


def _batch_product_is_liked(serializer: Any, product: Product) -> bool:
    root = getattr(serializer, 'root', serializer)
    context = getattr(root, 'context', None)
    if context is None:
        context = getattr(serializer, 'context', {}) or {}
    request = context.get("request")
    if not request or not request.user.is_authenticated:
        return False
    prefetched_likes = getattr(product, "user_likes", None)
    if prefetched_likes is not None:
        return bool(prefetched_likes)
    liked_ids = context.setdefault('user_liked_product_ids', None)
    if liked_ids is None:
        root_inst = getattr(root, 'instance', None)
        instances = None
        if root_inst:
            if isinstance(root, serializers.ListSerializer) or isinstance(root_inst, (list, tuple)):
                instances = root_inst
            elif hasattr(root_inst, 'cart_items'):
                instances = root_inst.cart_items.all()
        if instances:
            from core.models import Wishlist
            pks = []
            for inst in instances:
                if hasattr(inst, 'pk') and isinstance(inst, Product):
                    pks.append(inst.pk)
                elif hasattr(inst, 'product_id'):
                    pks.append(inst.product_id)
                elif hasattr(inst, 'product') and hasattr(inst.product, 'pk'):
                    pks.append(inst.product.pk)
            liked_ids = set(Wishlist.objects.filter(user=request.user, product_id__in=pks, liked=True).values_list('product_id', flat=True))
            context['user_liked_product_ids'] = liked_ids
    if liked_ids is not None:
        return product.pk in liked_ids
    return product.wishlist_set.filter(user=request.user).exists()


class ProductSerializer(TaggitSerializer, serializers.ModelSerializer):
    tags = TagListSerializerField()
    images = ProductImgSerializer(many=True, read_only=True)
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_upcoming_drop = serializers.BooleanField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "store",
            "title",
            "category",
            "available",
            "discount",
            "description",
            "price",
            "sale_price",
            "visibility",
                  'is_blocked', 'blocked_reason', 'blocked_at',
            "tags",
            "images",
            "average_rating",
            "likes_count",
            "is_liked",
            "is_digital",
            "label",
            "drop_at",
            "is_upcoming_drop",
        ]
        read_only_fields = ["is_blocked", "blocked_reason", "blocked_at", "sale_price", "average_rating", "likes_count", "is_liked", "is_upcoming_drop"]

    def to_representation(self, instance: Product) -> dict[str, Any]:
        data = super().to_representation(instance)
        if data.get('image_url'):
            data['image_url'] = normalize_media_url(data['image_url'])
        return data

    def get_likes_count(self, product: Product) -> int:
        return _batch_product_likes_count(self, product)

    def get_is_liked(self, product):
        return _batch_product_is_liked(self, product)


class ProductCardSerializer(serializers.ModelSerializer):
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_upcoming_drop = serializers.BooleanField(read_only=True)

    class Meta:
        model = Product

        fields = [
            "id",
            "title",
            "available",
            "discount",
            "label",
            "price",
            "sale_price",
            "average_rating",
            "likes_count",
            "is_liked",
            "is_digital",
            "drop_at",
            "is_upcoming_drop",
        ]

    def get_likes_count(self, product: Product) -> int:
        return _batch_product_likes_count(self, product)

    def get_is_liked(self, product):
        return _batch_product_is_liked(self, product)
