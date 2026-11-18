"""Refund title search must use the same tenant scope as serialized items."""
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from cart.models import Cart, CartItem
from core.models import Refund, User
from payment.models import Order
from product.models import Product
from store.models import Store
from store.serializers import MerchantRefundSerializer


@pytest.mark.django_db
@pytest.mark.parametrize("source", ["refund", "order", "legacy_cart"])
def test_refund_search_excludes_other_store_titles(source: str) -> None:
    buyer = User.objects.create_user(email="search-buyer@example.test", password="fixture-only")
    owners = [
        User.objects.create_user(email=f"search-owner-{index}@example.test", password="fixture-only")
        for index in range(2)
    ]
    stores = [
        Store.objects.create(
            user=owner, name=f"Search store {index}", status=Store.STATUS_ACTIVE,
            verified_at=timezone.now(),
        )
        for index, owner in enumerate(owners)
    ]
    products = [
        Product.objects.create(
            store=store, title=title, price=Decimal("10"), discount=0, available=5,
        )
        for store, title in zip(stores, ["Amber telescope", "Violet microscope"])
    ]
    cart = Cart.objects.create(user=buyer, ordered=True)
    for product in products:
        CartItem.objects.create(cart=cart, product=product, quantity=1)
    rows = [
        {
            "product": product.pk, "store": str(product.store_id),
            "title": product.title, "quantity": 1, "unit_price": "10.00",
            "line_total": "10.00", "is_digital": False,
        }
        for product in products
    ]
    order = Order.objects.create(
        user=buyer, cart=cart, ordered=True, status="delivered",
        total=20, paid_total=20,
        items_snapshot=rows if source != "legacy_cart" else [],
    )
    refund = Refund.objects.create(
        order=order, email=buyer.email, reason="Damaged shipment",
        refund_amount=20, selected_items=rows if source == "refund" else [],
    )
    if source != "legacy_cart":
        # Saved titles and ownership remain authoritative after catalog edits.
        Product.objects.filter(pk=products[0].pk).update(
            title="Renamed catalog instrument", store=stores[1],
        )
    client = APIClient()
    client.force_authenticate(owners[0])
    endpoint = f"/api/v1/stores/{stores[0].pk}/refunds/"
    permitted = client.get(endpoint, {"search": "AMBER", "limit": 1})
    assert permitted.status_code == 200, permitted.data
    assert permitted.data["count"] == 1
    record = permitted.data["results"][0]
    assert record["id"] == refund.pk
    assert [item["product"] for item in record["items"]] == [products[0].pk]
    serializer = MerchantRefundSerializer(data=record)
    assert serializer.is_valid(), serializer.errors

    excluded = client.get(endpoint, {"search": "Violet", "limit": 1})
    assert excluded.status_code == 200, excluded.data
    assert excluded.data["count"] == 0
    assert excluded.data["results"] == []
    if source != "legacy_cart":
        renamed = client.get(endpoint, {"search": "Renamed catalog"})
        assert renamed.status_code == 200, renamed.data
        assert renamed.data["count"] == 0

    # Ordinary metadata searches and authorization still apply.
    metadata = client.get(endpoint, {"search": "Damaged shipment"})
    assert metadata.status_code == 200, metadata.data
    assert metadata.data["count"] == 1
    outsider = User.objects.create_user(email="search-outsider@example.test", password="fixture-only")
    client.force_authenticate(outsider)
    assert client.get(endpoint, {"search": "AMBER"}).status_code in (403, 404)
