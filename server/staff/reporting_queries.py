"""Database-side historical order/store counts and treasury aggregation for PostgreSQL."""
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from django.conf import settings
from django.db import connection
from django.utils import timezone
from payment.models import Order, OrderFulfillment
from cart.models import CartItem
from product.models import Product
from store.models import Store, StoreEarningsLedger

if TYPE_CHECKING:
    from .views import TreasuryEarnings


def historical_treasury_report(
    *,
    created_gte: datetime | None = None,
    created_lt: datetime | None = None,
) -> tuple["TreasuryEarnings", "TreasuryEarnings"] | None:
    if connection.vendor != "postgresql":
        return None
    from .views import TreasuryEarnings

    quote = connection.ops.quote_name
    tbl_ledger, tbl_store, tbl_order, tbl_fulfillment = [
        quote(model._meta.db_table)
        for model in (StoreEarningsLedger, Store, Order, OrderFulfillment)
    ]

    where_parts = [
        "s.entry_type = 'sale'",
        "s.status IN ('pending', 'available', 'completed', 'reversed')",
    ]
    refund_where = [
        "entry_type = 'refund'",
        "status = 'completed'",
        "order_id IS NOT NULL",
    ]
    params: list[datetime] = []
    refund_params: list[datetime] = []
    if created_lt is not None:
        where_parts.append("s.created < %s")
        params.append(created_lt)
        refund_where.append("created < %s")
        refund_params.append(created_lt)
    if created_gte is not None:
        where_parts.append("s.created >= %s")
        params.append(created_gte)
        refund_where.append("created >= %s")
        refund_params.append(created_gte)

    where_sql = " AND ".join(where_parts)
    refund_where_sql = " AND ".join(refund_where)

    cutoff = timezone.now() - timedelta(days=settings.REFUND_WINDOW_DAYS)
    sql = f"""
        WITH refund_summary AS MATERIALIZED (
            SELECT order_id, store_id, 
                   SUM(fee_amount) AS fee_refund, 
                   SUM(net_amount) AS net_refund
            FROM {tbl_ledger}
            WHERE {refund_where_sql}
            GROUP BY order_id, store_id
        ),
        sales_base AS MATERIALIZED (
            SELECT 
                s.id,
                s.order_id,
                s.store_id,
                s.status AS sale_status,
                s.available_at,
                COALESCE(st.is_official, FALSE) AS is_official,
                s.fee_amount + COALESCE(r.fee_refund, 0) AS fee,
                s.net_amount + COALESCE(r.net_refund, 0) AS net,
                o.created AS order_created,
                o.delivered_at AS order_delivered_at,
                COALESCE(o.payment_disputed, FALSE) AS payment_disputed,
                r.fee_refund IS NOT NULL AS has_refund,
                COALESCE(f.delivered_at, o.delivered_at) AS effective_delivered_at,
                CASE 
                    WHEN o.items_snapshot IS NOT NULL AND jsonb_typeof(o.items_snapshot) = 'array' THEN
                        EXISTS (
                            SELECT 1 FROM jsonb_array_elements(o.items_snapshot) item
                            WHERE (item->>'store' IS NULL OR (item->>'store') = s.store_id::text)
                              AND COALESCE((item->>'is_digital')::boolean, FALSE) = FALSE
                        )
                    WHEN o.id IS NOT NULL THEN TRUE
                    ELSE FALSE
                END AS is_physical
            FROM {tbl_ledger} s
            LEFT JOIN {tbl_store} st ON st.id = s.store_id
            LEFT JOIN {tbl_order} o ON o.id = s.order_id
            LEFT JOIN refund_summary r ON r.order_id = s.order_id AND r.store_id = s.store_id
            LEFT JOIN {tbl_fulfillment} f ON f.order_id = s.order_id AND f.store_id = s.store_id
            WHERE {where_sql}
        ),
        evaluated_sales AS MATERIALIZED (
            SELECT 
                id, fee, net, is_official, sale_status,
                CASE 
                    WHEN sale_status = 'pending' THEN TRUE
                    WHEN order_id IS NOT NULL AND payment_disputed THEN TRUE
                    WHEN order_id IS NULL AND available_at IS NULL THEN (sale_status = 'pending')
                    WHEN is_physical AND effective_delivered_at IS NULL THEN TRUE
                    WHEN is_physical AND effective_delivered_at > %s THEN TRUE
                    WHEN order_created > %s THEN TRUE
                    WHEN available_at IS NOT NULL AND available_at > now() THEN TRUE
                    ELSE FALSE
                END AS is_held
            FROM sales_base
            WHERE NOT (sale_status = 'reversed' AND NOT has_refund)
        )
        SELECT 
            COALESCE(SUM(CASE WHEN NOT is_held AND NOT is_official THEN fee ELSE 0 END), 0) AS cleared_marketplace_commission,
            COALESCE(SUM(CASE WHEN NOT is_held AND is_official THEN net ELSE 0 END), 0) AS cleared_official_revenue,
            COALESCE(SUM(CASE WHEN is_held THEN net ELSE 0 END), 0) AS cleared_escrow,
            
            COALESCE(SUM(CASE WHEN NOT is_official THEN fee ELSE 0 END), 0) AS pending_marketplace_commission,
            COALESCE(SUM(CASE WHEN is_official THEN net ELSE 0 END), 0) AS pending_official_revenue,
            COALESCE(SUM(CASE WHEN is_held THEN net ELSE 0 END), 0) AS pending_escrow
        FROM evaluated_sales
        WHERE sale_status IN ('available', 'completed', 'reversed', 'pending');
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [*refund_params, *params, cutoff, cutoff])
        row = cursor.fetchone()
        cleared = TreasuryEarnings(
            marketplace_commission=row[0],
            official_revenue=row[1],
            escrow=row[2],
        )
        pending = TreasuryEarnings(
            marketplace_commission=row[3],
            official_revenue=row[4],
            escrow=row[5],
        )
        return cleared, pending


def historical_store_counts() -> dict[int, int] | None:
    if connection.vendor != "postgresql":
        return None
    quote = connection.ops.quote_name
    orders, items, products = [quote(model._meta.db_table) for model in (Order, CartItem, Product)]
    # Only trusted schema identifiers are interpolated; all values are parameters.
    sql = f"""
        WITH eligible AS (
            SELECT id, cart_id, items_snapshot FROM {orders}
            WHERE ordered AND status IN (%s, %s, %s, %s)
        ), pairs AS (
            SELECT o.id AS order_id,
                   CASE WHEN (item->>'store') ~ '^[0-9]{{1,18}}$'
                        THEN (item->>'store')::bigint END AS store_id
            FROM eligible o CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(o.items_snapshot) = 'array'
                     THEN o.items_snapshot ELSE '[]'::jsonb END
            ) AS item
            UNION
            SELECT o.id, p.store_id FROM eligible o
            JOIN {items} ci ON ci.cart_id = o.cart_id
            JOIN {products} p ON p.id = ci.product_id
            WHERE o.items_snapshot IS NULL OR o.items_snapshot = '[]'::jsonb
        )
        SELECT store_id, COUNT(DISTINCT order_id) FROM pairs
        WHERE store_id IS NOT NULL GROUP BY store_id
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, ["confirmed", "shipped", "delivered", "picked up"])
        return {int(store_id): int(count) for store_id, count in cursor.fetchall()}

