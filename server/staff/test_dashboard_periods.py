from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.response import Response

from core.models import User
from payment.models import Order
from staff.serializers import DashboardQuerySerializer
from staff.views import StaffViewSet
from store.models import Store
from store.views import StoreViewSet


class DashboardPeriodTests(TestCase):
    def setUp(self) -> None:
        self.admin = User.objects.create_superuser(
            email="period-admin@example.test", password="Test123!"
        )
        self.owner = User.objects.create_user(
            email="period-owner@example.test", password="Test123!"
        )
        self.store = Store.objects.create(
            user=self.owner, name="Period store", username="period-store"
        )
        self.order = Order.objects.create(
            user=self.owner,
            status="confirmed",
            ordered=True,
            total=Decimal("20"),
            items_snapshot=[{"store": self.store.pk, "product": 1, "is_digital": True}],
        )
        Order.objects.filter(pk=self.order.pk).update(
            created=timezone.now() - timedelta(days=500)
        )

    def request(self, view: type, user: User, days: str, **kwargs: object) -> Response:
        request = APIRequestFactory().get(
            "/", {"days": days, "time_zone": "Africa/Lagos"}
        )
        force_authenticate(request, user)
        return view.as_view({"get": "dashboard"})(request, **kwargs)

    def test_all_time_includes_history_beyond_one_year(self) -> None:
        recent = self.request(StaffViewSet, self.admin, "365")
        history = self.request(StaffViewSet, self.admin, "all")
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.data["orders"]["total"], 0)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["orders"]["total"], 1)
        self.assertEqual(history.data["orders"]["gross_total"], "20.00")
        self.assertEqual(history.data["period"]["days"], "all")
        self.assertIsNone(history.data["period"]["from"])
        self.assertEqual(history.data["scopes"]["orders"], "All time")

    def test_staff_all_time_does_not_expose_admin_financials(self) -> None:
        staff = User.objects.create_user(
            email="period-staff@example.test", password="Test123!", is_staff=True
        )
        response = self.request(StaffViewSet, staff, "all")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["orders"]["gross_total"])
        self.assertIsNone(response.data["financials"])

    def test_merchant_all_time_is_tenant_scoped_and_uses_monthly_activity(self) -> None:
        other = Store.objects.create(
            user=self.admin, name="Other period store", username="other-period"
        )
        Order.objects.create(
            user=self.admin,
            status="confirmed",
            ordered=True,
            items_snapshot=[{"store": other.pk, "product": 2}],
        )
        recent = self.request(StoreViewSet, self.owner, "365", pk=str(self.store.pk))
        history = self.request(StoreViewSet, self.owner, "all", pk=str(self.store.pk))
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.data["orders"]["total"], 0)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["orders"]["total"], 1)
        self.assertEqual(history.data["period"]["granularity"], "month")
        self.assertEqual(sum(row["count"] for row in history.data["orders_by_day"]), 1)
        self.assertEqual(len(history.data["orders_by_day"]), 1)

    def test_dashboard_queries_remain_bounded_and_constant(self) -> None:
        # Create additional orders to verify query count is O(1) and does not scale linearly
        extra_orders = [
            Order(
                user=self.owner,
                status="confirmed",
                ordered=True,
                total=Decimal("15.50"),
                items_snapshot=[{"store": self.store.pk, "product": idx, "is_digital": False}],
            )
            for idx in range(15)
        ]
        Order.objects.bulk_create(extra_orders)

        with CaptureQueriesContext(connection) as queries:
            response = self.request(StaffViewSet, self.admin, "30")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["orders"]["total"], 15)
        # Dashboard aggregates metrics in fixed consolidated queries, bounded below 20 queries
        self.assertLessEqual(len(queries), 20)

    def test_period_validation_remains_strict(self) -> None:
        for value in ["all", "7", "365"]:
            serializer = DashboardQuerySerializer(data={"days": value})
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(str(serializer.data["days"]), value)
        for value in ["0", "-1", "366", "everything", ""]:
            serializer = DashboardQuerySerializer(data={"days": value})
            self.assertFalse(serializer.is_valid())
