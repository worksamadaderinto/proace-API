from datetime import timedelta
from decimal import Decimal
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from cart.models import Cart, CartItem
from core.models import User
from payment.models import Order
from product.models import Product
from staff.views import treasury_earnings
from store.models import MerchantWallet, Store, StoreEarningsLedger
from store.services import pending_merchant_ledger_net


class LegacyTreasuryQueriesTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email="legacy-rollup@example.test", password="test")
        self.store = Store.objects.create(user=self.user, name="Legacy")
        self.wallet = MerchantWallet.objects.create(user=self.user)
        self.product = Product.objects.create(store=self.store, title="Physical", price=100, discount=0, available=20)

    def sale(self) -> None:
        cart = Cart.objects.create(user=self.user, ordered=True)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        order = Order.objects.create(user=self.user, cart=cart, ordered=True, status="delivered", items_snapshot=[], delivered_at=timezone.now()-timedelta(days=17))
        Order.objects.filter(pk=order.pk).update(created=timezone.now()-timedelta(days=20))
        StoreEarningsLedger.objects.create(wallet=self.wallet, store=self.store, order=order, entry_type="sale", status="available", gross_amount=100, fee_amount=4, net_amount=96, available_at=timezone.now()-timedelta(days=17))

    def test_legacy_sale_growth_does_not_add_per_order_cart_queries(self) -> None:
        self.sale()
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(treasury_earnings().marketplace_commission, Decimal("4"))
        for _ in range(11):
            self.sale()
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(treasury_earnings().marketplace_commission, Decimal("48"))
        self.assertLessEqual(len(large), len(small) + 1)

    def test_pending_refund_aggregation_is_bounded_and_store_scoped(self) -> None:
        self.sale()
        own_order = Order.objects.latest("pk")
        Order.objects.filter(pk=own_order.pk).update(payment_disputed=True)
        StoreEarningsLedger.objects.create(wallet=self.wallet, store=self.store, order=own_order, entry_type="refund", status="completed", net_amount=-20)
        other = Store.objects.create(user=self.user, name="Other ledger", username="other-ledger")
        StoreEarningsLedger.objects.create(wallet=self.wallet, store=other, order=own_order, entry_type="sale", status="pending", net_amount=1000)
        ledger = StoreEarningsLedger.objects.filter(store=self.store)
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(pending_merchant_ledger_net(ledger), Decimal("76"))
        for _ in range(11):
            self.sale()
        Order.objects.update(payment_disputed=True)
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(pending_merchant_ledger_net(ledger), Decimal("1132"))
        self.assertLessEqual(len(large), len(small) + 1)


    def test_treasury_narrow_reads_preserve_refunds_and_live_dispute_policy(self) -> None:
        self.sale()
        order = Order.objects.latest("pk")
        StoreEarningsLedger.objects.create(wallet=self.wallet, store=self.store, order=order, entry_type="refund", status="completed", fee_amount=-1, net_amount=-24)
        with CaptureQueriesContext(connection) as queries:
            totals = treasury_earnings()
        self.assertEqual(totals.marketplace_commission, Decimal("3"))
        self.assertEqual(totals.escrow, Decimal("0"))
        sql = " ".join(row["sql"] for row in queries.captured_queries)
        self.assertNotIn('"address_snapshot"', sql)
        self.assertNotIn('"buyer_email"', sql)
        self.assertNotIn('"stripe_payment_intent"', sql)
        Order.objects.filter(pk=order.pk).update(payment_disputed=True)
        totals = treasury_earnings()
        self.assertEqual(totals.marketplace_commission, Decimal("0"))
        self.assertEqual(totals.escrow, Decimal("72"))
        self.assertEqual(treasury_earnings(include_pending=True).marketplace_commission, Decimal("3"))
