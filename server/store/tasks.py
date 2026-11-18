from __future__ import annotations

import logging
from celery import shared_task
from store.services import publish_due_products, settle_pending_merchant_earnings, reconcile_expired_flash_deals

logger = logging.getLogger(__name__)


@shared_task(name="store.settle_pending_merchant_earnings_task")
def settle_pending_merchant_earnings_task() -> int:
    """
    Celery task to release merchant earnings held in review escrow
    into withdrawable available balance once the refund window has elapsed.
    """
    settled_count = settle_pending_merchant_earnings()
    if settled_count > 0:
        logger.info("Settled %d merchant ledger entries into available balance.", settled_count)
    return settled_count


@shared_task(name="store.publish_due_products_task")
def publish_due_products_task() -> int:
    """
    Celery task to publish scheduled products whose visibility timestamp is reached.
    """
    count = publish_due_products()
    if count > 0:
        logger.info("Published %d scheduled products.", count)
    return count


@shared_task(name="store.reconcile_expired_flash_deals_task")
def reconcile_expired_flash_deals_task() -> int:
    """
    Celery task to revert expired flash deals back to their base discount.
    """
    count = reconcile_expired_flash_deals()
    if count > 0:
        logger.info("Reconciled %d expired flash deals back to base discount.", count)
    return count


@shared_task(name="store.cleanup_digital_uploads_task")
def cleanup_digital_uploads_task() -> None:
    from django.core.management import call_command
    call_command('cleanup_digital_uploads')
