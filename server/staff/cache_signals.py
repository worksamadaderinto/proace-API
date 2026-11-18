"""Invalidate cached overview projections when their authoritative rows change."""

from typing import Any
import logging
from redis import RedisError

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from payment.models import Order, OrderFulfillment
from product.models import Product
from store.models import Store, StoreEarningsLedger

KEYS = (
    "backoffice:treasury_earnings:pending",
    "backoffice:treasury_earnings:cleared",
    "backoffice:historical_top_stores",
)


def clear_overview() -> None:
    try:
        from .treasury_cache import invalidate_report
        invalidate_report()
        cache.delete_many(KEYS)
    except (RedisError, OSError):
        logging.getLogger(__name__).warning(
            "Overview cache invalidation unavailable.", exc_info=True
        )


@receiver(post_save, sender=Order)
@receiver(post_delete, sender=Order)
@receiver(post_save, sender=OrderFulfillment)
@receiver(post_delete, sender=OrderFulfillment)
@receiver(post_save, sender=StoreEarningsLedger)
@receiver(post_delete, sender=StoreEarningsLedger)
@receiver(post_save, sender=Store)
@receiver(post_delete, sender=Store)
@receiver(post_save, sender=Product)
@receiver(post_delete, sender=Product)
def invalidate_overview(sender: type, using: str, **kwargs: Any) -> None:
    if kwargs.get("raw"):
        return
    instance = kwargs.get("instance")
    try:
        from .treasury_cache import invalidate_summary

        invalidate_summary(instance)
    except Exception:
        pass
    clear_overview()
    # Clear again after commit so an intervening reader cannot cache old data.
    if transaction.get_connection(using).in_atomic_block:
        transaction.on_commit(clear_overview, using=using)
