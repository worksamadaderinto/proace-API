"""Verify bounded O(1) database query growth across store, notification, and cart serialization."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from cart.models import Cart, CartItem
from core.models import User
from notification.models import Notification
from product.models import Product
from store.models import MerchantWallet, Store, StoreAddress, StoreEarningsLedger, StoreInfo, StoreTeamMember
from store.serializers import StoreSerializer
from storefront.serializers import PublicStoreSerializer


@override_settings(ROOT_URLCONF="storefront.urls")
class QueryBudgetPerformanceTests(TestCase):
    def setUp(self) -> None:
        self.owner = User.objects.create_user(email=f"query-owner-{uuid4().hex[:8]}@test.com", password="test", first_name="Owner", last_name="User")
        self.owner_wallet = MerchantWallet.objects.create(user=self.owner)
        self.client = APIClient()
        self.rf = RequestFactory()

    def test_public_store_serializer_has_bounded_query_growth(self) -> None:
        """Serializing 20 stores must execute in bounded O(1) queries instead of 4N queries."""
        stores_list: list[Store] = []
        for i in range(20):
            uid = uuid4().hex[:8]
            seller = User.objects.create_user(email=f"seller-{uid}@test.com", password="test")
            seller_wallet = MerchantWallet.objects.create(user=seller)
            store = Store.objects.create(
                user=seller,
                name=f"Budget Store {i}",
                username=f"bs-{uid}",
                status=Store.STATUS_ACTIVE,
                verified_at=timezone.now(),
            )
            StoreInfo.objects.create(store=store, bio=f"Bio {i}")
            StoreAddress.objects.create(store=store, city="Lagos", state="LA", country="NG", is_default=True)
            Product.objects.create(store=store, title=f"Item {i}", price=Decimal("10.00"), discount=0, available=5, visibility=True)
            StoreEarningsLedger.objects.create(
                wallet=seller_wallet,
                store=store,
                entry_type=StoreEarningsLedger.TYPE_SALE,
                status=StoreEarningsLedger.STATUS_AVAILABLE,
                gross_amount=Decimal("100.00"),
                fee_amount=Decimal("15.00"),
                net_amount=Decimal("85.00"),
            )
            stores_list.append(store)

        # Baseline: serialize 1 store
        with CaptureQueriesContext(connection) as small_queries:
            data_one = PublicStoreSerializer([stores_list[0]], many=True).data
        self.assertEqual(len(data_one), 1)
        self.assertEqual(data_one[0]["product_count"], 1)

        # Scale test: serialize 20 stores
        with CaptureQueriesContext(connection) as large_queries:
            data_twenty = PublicStoreSerializer(stores_list, many=True).data
        self.assertEqual(len(data_twenty), 20)
        self.assertEqual(data_twenty[19]["product_count"], 1)

        # In an N+1 scenario, 20 stores would issue 80+ queries (4 per store).
        # With batching, query growth is strictly bounded and independent of row count.
        self.assertLessEqual(len(large_queries), len(small_queries) + 1)
        self.assertLessEqual(len(large_queries), 6)

    def test_merchant_store_list_has_bounded_query_growth(self) -> None:
        """Listing merchant stores must not execute per-store queries for tiers or user roles."""
        assistant = User.objects.create_user(email=f"query-assistant-{uuid4().hex[:8]}@test.com", password="test")
        stores: list[Store] = []
        for i in range(15):
            uid = uuid4().hex[:8]
            store = Store.objects.create(
                user=self.owner,
                name=f"Merchant Store {i}",
                username=f"ms-{uid}",
                status=Store.STATUS_ACTIVE,
                verified_at=timezone.now(),
            )
            StoreEarningsLedger.objects.create(
                wallet=self.owner_wallet,
                store=store,
                entry_type=StoreEarningsLedger.TYPE_SALE,
                status=StoreEarningsLedger.STATUS_AVAILABLE,
                gross_amount=Decimal("50.00"),
                fee_amount=Decimal("7.50"),
                net_amount=Decimal("42.50"),
            )
            StoreTeamMember.objects.create(
                store=store,
                user=assistant,
                role=StoreTeamMember.ROLE_STORE_ADMIN,
                status=StoreTeamMember.STATUS_ACTIVE,
            )
            stores.append(store)

        request = self.rf.get("/stores/")
        request.user = assistant

        # Preload user relation to mirror StoreViewSet select_related('user').prefetch_related('assigned_staff')
        prefetched_stores = list(Store.objects.select_related("user").prefetch_related("assigned_staff").filter(pk__in=[s.pk for s in stores]))

        with CaptureQueriesContext(connection) as queries:
            data = StoreSerializer(prefetched_stores, many=True, context={"request": request}).data

        self.assertEqual(len(data), 15)
        # Without fix, 15 stores = 30+ queries (tiers, user_roles). With batching <= 3 queries.
        self.assertLessEqual(len(queries), 3)

    def test_notification_list_has_bounded_query_growth(self) -> None:
        """Listing notifications must not execute per-row store or GenericForeignKey queries."""
        self.client.force_authenticate(self.owner)
        store = Store.objects.create(user=self.owner, name="Notif Store", username=f"ns-{uuid4().hex[:8]}", status="active")
        for i in range(15):
            n = Notification.objects.create(
                recipient=self.owner,
                verb=f"Order {i} placed",
                actor=self.owner,
                target=store,
            )
            n.stores.add(store)

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/notifications/")
        self.assertEqual(response.status_code, 200)
        # Without fix, 15 notifications = 60+ queries (stores, actor, target). With prefetch <= 8 queries.
        self.assertLessEqual(len(queries), 8)

    def test_cart_total_has_bounded_query_growth(self) -> None:
        """Cart total calculation must not execute separate queries per cart item."""
        cart = Cart.objects.create(user=self.owner, ordered=False)
        store = Store.objects.create(user=self.owner, name="Cart Store", username=f"cs-{uuid4().hex[:8]}", status="active")
        for i in range(10):
            product = Product.objects.create(store=store, title=f"Cart Product {i}", price=Decimal("10.00"), discount=0, available=10)
            CartItem.objects.create(cart=cart, product=product, quantity=2)

        from cart.serializers import CartSerializer
        prefetched_cart = Cart.objects.prefetch_related("cart_items__product").get(pk=cart.pk)

        with CaptureQueriesContext(connection) as queries:
            total = CartSerializer(prefetched_cart).data["total"]

        self.assertEqual(Decimal(str(total)), Decimal("200.00"))
        # Using already-prefetched items and batching likes, query count is bounded (at most 1 batch query)
        self.assertLessEqual(len(queries), 2)

    def test_staff_coupons_query_budget_is_constant_with_redemptions(self) -> None:
        """Listing backoffice coupons with redemptions must scale in O(1) queries."""
        from datetime import timedelta
        from payment.models import Coupon, CouponRedemption, Order
        from staff.views import StaffViewSet
        from rest_framework.test import force_authenticate

        admin = User.objects.create_superuser(
            email=f"staff-admin-{uuid4().hex[:8]}@test.com", password="password"
        )
        buyer = User.objects.create_user(
            email=f"coupon-buyer-{uuid4().hex[:8]}@test.com", password="password"
        )

        # Baseline: 1 coupon with 2 redemptions
        c1 = Coupon.objects.create(
            code=f"COUPON_1_{uuid4().hex[:6]}",
            discount=10,
            created_by=admin,
            valid_to=timezone.now() + timedelta(days=30),
            num_available=10,
        )
        o1 = Order.objects.create(user=buyer, total=10)
        CouponRedemption.objects.create(coupon=c1, user=buyer, order=o1)

        rf = RequestFactory()
        view = StaffViewSet.as_view({"get": "coupons"})

        req1 = rf.get("/api/staff/coupons/")
        force_authenticate(req1, user=admin)
        with CaptureQueriesContext(connection) as small:
            resp1 = view(req1)
        self.assertEqual(resp1.status_code, 200)

        # Scale test: 10 coupons with multiple redemptions
        for i in range(2, 11):
            c = Coupon.objects.create(
                code=f"COUPON_{i}_{uuid4().hex[:6]}",
                discount=10,
                created_by=admin,
                valid_to=timezone.now() + timedelta(days=30),
                num_available=10,
            )
            o = Order.objects.create(user=buyer, total=10)
            CouponRedemption.objects.create(coupon=c, user=buyer, order=o)

        req2 = rf.get("/api/staff/coupons/")
        force_authenticate(req2, user=admin)
        with CaptureQueriesContext(connection) as large:
            resp2 = view(req2)
        self.assertEqual(resp2.status_code, 200)

        # In unoptimized N+1, 10 coupons with redemptions would issue 50+ queries.
        # With prefetch_related, query growth is strictly constant (at most 3 queries).
        self.assertEqual(len(large), len(small))
        self.assertLessEqual(len(large), 4)

    def test_staff_stores_query_budget_is_constant(self) -> None:
        """Listing backoffice stores must scale in O(1) queries regardless of store count."""
        from staff.views import StaffViewSet
        from rest_framework.test import force_authenticate

        admin = User.objects.create_superuser(
            email=f"stores-admin-{uuid4().hex[:8]}@test.com", password="password"
        )
        rf = RequestFactory()
        view = StaffViewSet.as_view({"get": "stores"})

        for i in range(10):
            u = User.objects.create_user(email=f"backoffice-vendor-{i}-{uuid4().hex[:6]}@test.com", password="password")
            MerchantWallet.objects.create(user=u)
            Store.objects.create(user=u, name=f"Staff Store {i}", username=f"ss-{uuid4().hex[:6]}", status="active")

        req = rf.get("/api/staff/stores/")
        force_authenticate(req, user=admin)
        with CaptureQueriesContext(connection) as ctx:
            resp = view(req)
        self.assertEqual(resp.status_code, 200)
        # Without fix: 50+ queries. With prefetch and batched user roles: bounded to 7 queries.
        self.assertLessEqual(len(ctx), 8)

    def test_merchant_refunds_query_budget_is_constant_with_legacy_orders(self) -> None:
        """Listing merchant refunds for unsnapshotted orders must scale in O(1) queries."""
        from core.models import Refund
        from payment.models import Order
        from store.views import StoreViewSet
        from rest_framework.test import force_authenticate

        vendor = User.objects.create_user(email=f"refund-vendor-{uuid4().hex[:8]}@test.com", password="password")
        store = Store.objects.create(user=vendor, name="Refund Test Store", username=f"ref-store-{uuid4().hex[:6]}", status="active")
        MerchantWallet.objects.create(user=vendor)
        prod = Product.objects.create(store=store, title="Refund Item", price=Decimal("15.00"), discount=0, available=10)

        buyer = User.objects.create_user(email=f"refund-buyer-{uuid4().hex[:8]}@test.com", password="password")
        for i in range(10):
            cart = Cart.objects.create(user=buyer)
            CartItem.objects.create(cart=cart, product=prod, quantity=1)
            order = Order.objects.create(user=buyer, total=Decimal("15.00"), cart=cart)
            Refund.objects.create(order=order, email=buyer.email, reason="Damage", refund_amount=Decimal("15.00"), selected_items=[])

        rf = RequestFactory()
        req = rf.get(f"/api/stores/{store.pk}/refunds/")
        force_authenticate(req, user=vendor)
        view = StoreViewSet.as_view({"get": "refunds"})

        with CaptureQueriesContext(connection) as ctx:
            resp = view(req, pk=store.pk)
        self.assertEqual(resp.status_code, 200)
        # Without fix: 2 queries per refund (20+ queries). With prefetch: bounded to 6 queries.
        self.assertLessEqual(len(ctx), 7)

    def test_merchant_product_analytics_query_budget_is_constant(self) -> None:
        """Product analytics endpoint must execute in bounded O(1) queries regardless of order count."""
        from core.models import Refund, Wishlist, Review
        from payment.models import Order
        from store.views import MerchantProductViewSet
        from rest_framework.test import force_authenticate

        vendor = User.objects.create_user(email=f"analytics-vendor-{uuid4().hex[:8]}@test.com", password="password")
        store = Store.objects.create(user=vendor, name="Analytics Store", username=f"analytics-{uuid4().hex[:6]}", status="active")
        MerchantWallet.objects.create(user=vendor)
        product = Product.objects.create(
            store=store,
            title="Analytics Pro Item",
            price=Decimal("29.99"),
            discount=0,
            available=50,
            visibility=True,
        )

        buyer = User.objects.create_user(email=f"analytics-buyer-{uuid4().hex[:8]}@test.com", password="password")
        # Create wishlist & review
        Wishlist.objects.create(user=buyer, product=product, liked=True)
        Review.objects.create(user=buyer, product=product, label="Great", comment="Superb", rating=5)

        # Create 10 orders with items_snapshot
        for i in range(10):
            Order.objects.create(
                user=buyer,
                total=Decimal("59.98"),
                ordered=True,
                status="confirmed",
                items_snapshot=[{
                    "product": product.id,
                    "product_title": product.title,
                    "quantity": 2,
                    "unit_price": "29.99",
                    "line_total": "59.98",
                    "variant_name": "Size L" if i % 2 == 0 else "Size M",
                    "store": store.id,
                }],
            )

        # Create 5 legacy orders with cart items
        for i in range(5):
            cart = Cart.objects.create(user=buyer)
            CartItem.objects.create(cart=cart, product=product, quantity=1, variant="Size S")
            Order.objects.create(user=buyer, total=Decimal("29.99"), ordered=True, status="delivered", cart=cart)

        rf = RequestFactory()
        req = rf.get(f"/api/v1/stores/{store.pk}/products/{product.pk}/analytics/?days=30")
        force_authenticate(req, user=vendor)
        view = MerchantProductViewSet.as_view({"get": "analytics"})

        with CaptureQueriesContext(connection) as ctx:
            resp = view(req, store_pk=store.pk, pk=product.pk)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["summary"]["units_sold"], 25) # 10*2 + 5*1
        self.assertEqual(resp.data["summary"]["orders_count"], 15)
        self.assertEqual(resp.data["summary"]["wishlist_count"], 1)
        self.assertEqual(resp.data["summary"]["total_reviews"], 1)
        self.assertGreater(len(resp.data["timeline"]), 0)
        self.assertGreaterEqual(len(resp.data["variants"]), 2)
        # Bounded O(1) query count (no per-order or per-item queries)
        self.assertLessEqual(len(ctx), 10)


