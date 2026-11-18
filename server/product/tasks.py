"""Background Elasticsearch synchronization for catalog writes."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from product.models import SearchIndexJob, SearchIndexExecutionLock

from product.search import delete_product, enabled, index_product, index_store_products

logger = logging.getLogger("codematics.search")


def _execute_intent(action: str, target_id: int, operation: Callable[..., None]) -> None:
    if not enabled():
        return
    # Requests update intent rows only. Lock a separate worker-only row so
    # Elasticsearch latency cannot block a catalog write refreshing its intent.
    lock_action = SearchIndexJob.INDEX_PRODUCT if action == SearchIndexJob.DELETE_PRODUCT else action
    lock, _ = SearchIndexExecutionLock.objects.get_or_create(action=lock_action, target_id=target_id)
    with transaction.atomic():
        SearchIndexExecutionLock.objects.select_for_update().get(pk=lock.pk)
        job = SearchIndexJob.objects.filter(
            action=action, target_id=target_id
        ).first()
        if job is None:
            return
        revision = job.revision
        operation(target_id, strict=True)
        SearchIndexJob.objects.filter(pk=job.pk, revision=revision).delete()


@shared_task(
    ignore_result=True, name="product.tasks.index_product_task",
    autoretry_for=(Exception,), retry_backoff=5, retry_backoff_max=300,
    retry_jitter=True, max_retries=5,
)
def index_product_task(product_id: int) -> None:
    _execute_intent(SearchIndexJob.INDEX_PRODUCT, product_id, index_product)


@shared_task(
    ignore_result=True, name="product.tasks.index_store_products_task",
    autoretry_for=(Exception,), retry_backoff=5, retry_backoff_max=300,
    retry_jitter=True, max_retries=5,
)
def index_store_products_task(store_id: int) -> None:
    _execute_intent(SearchIndexJob.INDEX_STORE, store_id, index_store_products)


@shared_task(
    ignore_result=True, name="product.tasks.delete_product_task",
    autoretry_for=(Exception,), retry_backoff=5, retry_backoff_max=300,
    retry_jitter=True, max_retries=5,
)
def delete_product_task(product_id: int) -> None:
    _execute_intent(SearchIndexJob.DELETE_PRODUCT, product_id, delete_product)


@shared_task(ignore_result=True, name="product.tasks.recover_search_index_jobs")
def recover_search_index_jobs(limit: int = 100) -> int:
    """Rotate a bounded batch fairly; publishing does not clear an intent."""
    if not enabled():
        return 0
    from notification.tasks import index_notification_task
    tasks = {
        SearchIndexJob.SYNC_NOTIFICATION: index_notification_task,
        SearchIndexJob.INDEX_PRODUCT: index_product_task,
        SearchIndexJob.INDEX_STORE: index_store_products_task,
        SearchIndexJob.DELETE_PRODUCT: delete_product_task,
    }
    jobs = list(
        SearchIndexJob.objects.order_by("updated_at", "pk")[:max(0, min(limit, 100))]
    )
    published = 0
    for job in jobs:
        # Rotate failed publications too, preventing starvation by a bad batch.
        SearchIndexJob.objects.filter(pk=job.pk, revision=job.revision).update(
            updated_at=timezone.now()
        )
        try:
            tasks[job.action].apply_async(args=(job.target_id,))
            published += 1
        except Exception:
            logger.exception("Unable to recover search intent", extra={"job_id": job.pk})
    return published


@shared_task(
    ignore_result=True,
    name="product.tasks.generate_digital_preview_task",
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_backoff_max=120,
    max_retries=3,
)
def generate_digital_preview_task(product_id: int, storage_key: str) -> None:
    """Generate a protected watermarked preview derivative in the background."""
    from django.core.files.storage import default_storage
    from product.models import Product
    from utils.digital_previews import create_digital_preview_from_storage
    from utils.functions import normalize_media_url

    with transaction.atomic():
        product = Product.objects.select_for_update().filter(pk=product_id).first()
        if not product:
            return
        # If product was modified or already ready with a newer file, skip
        if (not product.is_digital or product.digital_file_url != storage_key
                or product.preview_status == Product.PREVIEW_STATUS_READY):
            return
        product.preview_status = Product.PREVIEW_STATUS_PROCESSING
        product.save(update_fields=["preview_status", "updated"])

    try:
        preview_path = create_digital_preview_from_storage(storage_key, product.store_id, product.pk)
        if not preview_path:
            with transaction.atomic():
                product = Product.objects.select_for_update().filter(pk=product_id).first()
                if product and product.is_digital and product.digital_file_url == storage_key:
                    product.preview_status = Product.PREVIEW_STATUS_FAILED
                    product.preview_error = "Unable to create a protected preview from this asset."
                    product.save(update_fields=["preview_status", "preview_error", "updated"])
            return

        raw_url = default_storage.url(preview_path)
        public_image_url = normalize_media_url(raw_url)

        with transaction.atomic():
            product = Product.objects.select_for_update().filter(pk=product_id).first()
            if not product or not product.is_digital or product.digital_file_url != storage_key:
                return
            product.image_url = public_image_url
            product.digital_preview_watermark = True
            product.preview_status = Product.PREVIEW_STATUS_READY
            product.preview_error = ""
            product.save(update_fields=[
                "image_url",
                "digital_preview_watermark",
                "preview_status",
                "preview_error",
                "updated",
            ])

        from store.views import invalidate_catalog
        invalidate_catalog(product)
    except Exception:
        logger.exception("Failed to generate digital preview for product %s", product_id)
        with transaction.atomic():
            prod = Product.objects.select_for_update().filter(pk=product_id).first()
            if prod and prod.is_digital and prod.digital_file_url == storage_key:
                prod.preview_status = Product.PREVIEW_STATUS_PENDING
                prod.preview_error = "Preview processing is delayed. Please try again shortly."
                prod.save(update_fields=["preview_status", "preview_error", "updated"])
        raise



def schedule_digital_preview(product_id: int, storage_key: str) -> None:
    def publish() -> None:
        try:
            generate_digital_preview_task.apply_async(args=(product_id, storage_key), retry=False)
        except Exception:
            # Product pending state is the durable recovery intent.
            logger.exception("Unable to enqueue preview", extra={"product_id": product_id})
    transaction.on_commit(publish)


@shared_task(ignore_result=True, name="product.tasks.recover_digital_previews")
def recover_digital_previews(limit: int = 100) -> int:
    from product.models import Product

    stale = timezone.now() - timedelta(minutes=5)
    rows = list(Product.objects.filter(
        is_digital=True,
        preview_status__in=[Product.PREVIEW_STATUS_PENDING, Product.PREVIEW_STATUS_PROCESSING],
        updated__lte=stale,
    ).exclude(digital_file_url="").order_by("updated", "pk").values_list("pk", "digital_file_url")[:max(0, min(limit, 100))])
    for product_id, storage_key in rows:
        Product.objects.filter(pk=product_id, digital_file_url=storage_key).update(updated=timezone.now())
        schedule_digital_preview(product_id, storage_key)
    return len(rows)


@shared_task(ignore_result=True, name="product.tasks.dispatch_wishlist_alerts_task")
def dispatch_wishlist_alerts_task(
    product_id: int,
    previous_price: str | None = None,
    previous_stock: int | None = None,
) -> int:
    """Evaluate and dispatch restock and price-drop alerts to wishlist subscribers."""
    from decimal import Decimal
    from django.db.models import Q
    from core.models import Wishlist
    from product.models import Product
    from notification.dispatcher import DatabaseNotificationDispatcher

    product = Product.objects.filter(pk=product_id, visibility=True, is_blocked=False).select_related("store").first()
    if not product:
        return 0

    now = timezone.now()
    cutoff = now - timedelta(hours=24)
    current_price = product.price
    current_stock = product.available

    prev_price = Decimal(str(previous_price)) if previous_price is not None else None
    prev_stock = int(previous_stock) if previous_stock is not None else None

    is_restocked = prev_stock is not None and prev_stock <= 0 and current_stock > 0
    is_price_drop = prev_price is not None and current_price < prev_price

    if not is_restocked and not is_price_drop:
        return 0

    wishlists = (
        Wishlist.objects.filter(product=product, liked=True)
        .filter(Q(last_notified_at__isnull=True) | Q(last_notified_at__lt=cutoff))
        .select_related("user")
    )

    dispatcher = DatabaseNotificationDispatcher()
    notified_ids: list[int] = []

    for wl in wishlists:
        recipient = wl.user
        if recipient.pk == product.store.user_id:
            continue

        if is_restocked and wl.notify_restock:
            dispatcher.notify(
                recipient=recipient,
                verb="is back in stock!",
                target=product,
                actor=product.store,
                description=f"Great news! '{product.title}' is back in stock.",
                level="info",
                data={
                    "event": "restock",
                    "category": "product",
                    "product_id": product.pk,
                    "title": product.title,
                    "price": str(current_price),
                    "url": f"/products/{product.pk}",
                },
            )
            notified_ids.append(wl.pk)
        elif is_price_drop and wl.notify_price_drop:
            if wl.saved_price is None or current_price < wl.saved_price:
                diff = (prev_price - current_price).quantize(Decimal(".01"))
                dispatcher.notify(
                    recipient=recipient,
                    verb=f"dropped in price by ${diff}!",
                    target=product,
                    actor=product.store,
                    description=f"'{product.title}' dropped to ${current_price} (was ${prev_price}).",
                    level="info",
                    data={
                        "event": "price_drop",
                        "category": "product",
                        "product_id": product.pk,
                        "title": product.title,
                        "price": str(current_price),
                        "previous_price": str(prev_price),
                        "price_drop": str(diff),
                        "url": f"/products/{product.pk}",
                    },
                )
                notified_ids.append(wl.pk)

    if notified_ids:
        Wishlist.objects.filter(pk__in=notified_ids).update(last_notified_at=now)

    return len(notified_ids)
