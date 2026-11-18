"""Store-scoped financial, shipment and catalog regression coverage."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework.response import Response

from core.models import User, Refund
from payment.models import Order
from product.models import Product
from store.models import Store, StoreTeamMember
from storefront.serializers import PublicStoreSerializer

pytestmark = pytest.mark.django_db


def setup() -> tuple[User, User, Store, Product, APIClient]:
    owner = User.objects.create_user(
        email="audit2-owner@example.test", password="fixture-only"
    )
    buyer = User.objects.create_user(
        email="audit2-buyer@example.test", password="fixture-only"
    )
    store = Store.objects.create(
        user=owner, name="Audit store", status="active", verified_at=timezone.now()
    )
    product = Product.objects.create(
        store=store, title="Audit product", price=Decimal("10"), discount=0, available=5
    )
    client = APIClient()
    client.force_authenticate(owner)
    return owner, buyer, store, product, client


def row(product: Product, amount: str = "10.00") -> dict[str, object]:
    return {
        "product": product.pk,
        "store": product.store_id,
        "title": product.title,
        "unit_price": amount,
        "quantity": 1,
        "line_total": amount,
        "net_total": amount,
        "is_digital": False,
    }


def test_dashboard_hides_financials_from_fulfillment_role() -> None:
    owner, buyer, store, product, client = setup()
    StoreTeamMember.objects.create(
        store=store, user=buyer, role=StoreTeamMember.ROLE_FULFILLMENT_SPECIALIST
    )
    client.force_authenticate(buyer)
    assert client.get(f"/api/v1/stores/{store.pk}/payouts/").status_code == 403
    result = client.get(f"/api/v1/stores/{store.pk}/dashboard/")
    assert result.status_code == 200, result.data
    assert result.data["wallet"] == {}


def test_mixed_store_shipping_email_uses_store_tracking_link() -> None:
    owner, buyer, store, product, client = setup()
    other = Store.objects.create(
        user=buyer, name="Other store", status="active", verified_at=timezone.now()
    )
    other_product = Product.objects.create(
        store=other, title="Other item", price=Decimal("10"), discount=0, available=5
    )
    order = Order.objects.create(
        user=buyer,
        ordered=True,
        status="confirmed",
        total=20,
        paid_total=20,
        buyer_email=buyer.email,
        items_snapshot=[row(product), row(other_product)],
    )
    with patch("utils.functions.send_mail") as sender:
        result = client.post(
            f"/api/v1/stores/{store.pk}/orders/{order.pk}/tracking/",
            {
                "carrier": "FEDEX",
                "tracking_number": "fixture-shipment-1",
                "status": "shipped",
            },
            format="json",
        )
    assert result.status_code == 200, result.data
    assert "fixture-shipment-1" in result.data["tracking_url"]
    shipped = next(
        call for call in sender.call_args_list if call.args[0] == "order-shipped"
    )
    assert shipped.args[2]["tracking_number"] == "fixture-shipment-1"
    assert "fixture-shipment-1" in shipped.args[2]["tracking_url"]


def test_military_destination_rejects_non_usps_carrier() -> None:
    owner, buyer, store, product, client = setup()
    order = Order.objects.create(
        user=buyer,
        ordered=True,
        status="confirmed",
        total=10,
        paid_total=10,
        buyer_email=buyer.email,
        address_snapshot={
            "city": "APO",
            "state": "AE",
            "country": "US",
            "zip": "09012",
        },
        items_snapshot=[row(product)],
    )
    with patch("utils.functions.send_mail"):
        result = client.post(
            f"/api/v1/stores/{store.pk}/orders/{order.pk}/tracking/",
            {
                "carrier": "DHL",
                "tracking_number": "fixture-shipment-2",
                "status": "shipped",
            },
            format="json",
        )
    assert result.status_code == 400, result.data
    assert not order.fulfillments.filter(carrier="DHL").exists()


def test_public_store_count_excludes_blocked_products() -> None:
    owner, buyer, store, product, client = setup()
    product.is_blocked = True
    product.save(update_fields=["is_blocked"])
    from storefront.views import catalog

    assert catalog().filter(store=store).count() == 0
    assert PublicStoreSerializer(store).data["product_count"] == 0


def test_merchant_refund_payload_hides_other_stores_refund_amount() -> None:
    owner, buyer, store, product, client = setup()
    other = Store.objects.create(user=buyer, name="Other store")
    other_product = Product.objects.create(
        store=other, title="Other item", price=Decimal("100"), discount=0, available=5
    )
    order = Order.objects.create(
        user=buyer,
        ordered=True,
        status="delivered",
        total=110,
        paid_total=110,
        items_snapshot=[row(product), row(other_product, "100.00")],
    )
    Refund.objects.create(
        order=order,
        email=buyer.email,
        reason="Fixture",
        refund_amount=110,
        selected_items=order.items_snapshot,
    )
    result = client.get(f"/api/v1/stores/{store.pk}/refunds/")
    assert result.status_code == 200, result.data
    record = result.data["results"][0]
    assert Decimal(record["amount"]) == Decimal("10")
    assert Decimal(record["total_order_refund_amount"]) == Decimal("10")
    assert len(record["items"]) == 1


def test_pending_tracking_correction_preserves_status_and_store_scope() -> None:
    owner, buyer, store, product, client = setup()
    other = Store.objects.create(user=buyer, name="Other", status="active")
    other_product = Product.objects.create(
        store=other, title="Other", price=10, discount=0, available=5
    )
    order = Order.objects.create(
        user=buyer, ordered=True, status="pending", total=20,
        items_snapshot=[row(product), row(other_product)],
    )
    from store.views import StoreViewSet

    factory = APIRequestFactory()
    view = StoreViewSet.as_view({"patch": "update_order_tracking"})

    def submit(actor: User, payload: dict[str, str]) -> Response:
        request = factory.patch("/", payload, format="json")
        force_authenticate(request, user=actor)
        return view(request, pk=str(store.pk), order_pk=str(order.pk))

    with patch("utils.functions.send_mail"):
        response = submit(owner, {
            "status": "pending", "carrier": "USPS",
            "tracking_number": "fixture-pending",
        })
    assert response.status_code == 200, response.data
    order.refresh_from_db()
    assert order.status == "pending"
    assert order.fulfillments.get(store=store).tracking_number == "fixture-pending"
    assert order.fulfillments.get(store=other).tracking_number == ""
    order.fulfillments.filter(store=store).update(status="shipped")
    response = submit(owner, {"status": "pending", "tracking_number": "fixture-new"})
    assert response.status_code == 400
    assert order.fulfillments.get(store=store).status == "shipped"
    assert submit(buyer, {"status": "delivered"}).status_code in {403, 404}

    with patch("utils.functions.send_mail"):
        response = submit(owner, {
            "status": "delivered", "carrier": "USPS",
            "tracking_number": "fixture-delivered",
        })
    assert response.status_code == 200, response.data
    assert order.fulfillments.get(store=store).status == "delivered"
    for target in ("shipped", "cancelled", "refunded"):
        response = submit(owner, {"status": target})
        assert response.status_code == 400
    assert order.fulfillments.get(store=other).status == "pending"
