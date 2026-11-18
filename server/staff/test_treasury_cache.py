from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import User
from payment.models import Order
from staff import views
from store.models import MerchantWallet, Store, StoreEarningsLedger


class TreasuryReportTests(TransactionTestCase):
    def setUp(self) -> None:
        cache.clear()
        user = User.objects.create_user(email="treasury-report@example.test", password="test")
        self.store = Store.objects.create(user=user, name="Report fixture")
        self.wallet = MerchantWallet.objects.create(user=user)
        self.order = Order.objects.create(user=user, ordered=True, status="delivered", items_snapshot=[{"store": self.store.pk, "is_digital": True}])
        Order.objects.filter(pk=self.order.pk).update(created=timezone.now()-timedelta(days=17))
        self.sale = StoreEarningsLedger.objects.create(wallet=self.wallet, store=self.store, order=self.order, entry_type="sale", status="available", fee_amount=4, net_amount=96)

    def tearDown(self) -> None:
        cache.clear()

    def refund(self) -> None:
        StoreEarningsLedger.objects.create(wallet=self.wallet, store=self.store, order=self.order, entry_type="refund", status="completed", fee_amount=-1, net_amount=-24)

    def test_dashboard_modes_share_one_actual_scan(self) -> None:
        with patch("staff.views.treasury_report", wraps=views.treasury_report) as build:
            self.assertEqual(views.cached_treasury_earnings().total, Decimal("4"))
            self.assertEqual(views.cached_treasury_earnings(include_pending=True).total, Decimal("4"))
        self.assertEqual(build.call_count, 1)

    def test_refund_and_dispute_changes_invalidate_both_modes(self) -> None:
        self.assertEqual(views.cached_treasury_earnings().total, Decimal("4"))
        self.refund()
        self.assertEqual(views.cached_treasury_earnings().total, Decimal("3"))
        self.order.payment_disputed = True
        self.order.save(update_fields=["payment_disputed"])
        self.assertEqual(views.cached_treasury_earnings().total, Decimal("0"))
        self.assertEqual(views.cached_treasury_earnings(include_pending=True).total, Decimal("3"))
        self.assertEqual(views.cached_treasury_earnings().escrow, Decimal("72"))

    def test_write_during_scan_does_not_publish_obsolete_result(self) -> None:
        from staff.treasury_cache import read_report
        def racing_build():
            result = views.treasury_report()
            self.refund()
            return result
        self.assertEqual(read_report(racing_build)[0].total, Decimal("4"))
        self.assertEqual(views.cached_treasury_earnings().total, Decimal("3"))

    def test_uncommitted_snapshot_never_leaks_to_cache(self) -> None:
        with transaction.atomic():
            self.refund()
            self.assertEqual(views.cached_treasury_earnings().total, Decimal("3"))
            transaction.set_rollback(True)
        self.assertEqual(views.cached_treasury_earnings().total, Decimal("4"))

    def test_authoritative_financial_read_bypasses_stale_dashboard_cache(self) -> None:
        self.assertEqual(views.cached_treasury_earnings().total, Decimal("4"))
        # Bulk writers bypass save signals; display cache is bounded, never authoritative.
        StoreEarningsLedger.objects.filter(pk=self.sale.pk).update(fee_amount=9)
        self.assertEqual(views.treasury_earnings().total, Decimal("9"))

    def test_cache_expiry_rechecks_time_based_escrow_without_model_writes(self) -> None:
        import time
        now = timezone.now()
        Order.objects.filter(pk=self.order.pk).update(created=now)
        self.assertEqual(views.cached_treasury_earnings().total, Decimal("0"))
        with patch("staff.views.timezone.now", return_value=now+timedelta(days=15)), patch("django.core.cache.backends.locmem.time.time", return_value=time.time()+61):
            self.assertEqual(views.cached_treasury_earnings().total, Decimal("4"))
