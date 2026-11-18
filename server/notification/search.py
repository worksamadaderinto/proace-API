from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.conf import settings
from django.db import transaction

from notification.models import Notification
from product.models import SearchIndexJob

logger = logging.getLogger("codematics.notification_search")


def enabled() -> bool:
    return bool(getattr(settings, "ELASTICSEARCH_ENABLED", False))


def client() -> Any:
    from elasticsearch import Elasticsearch

    return Elasticsearch(
        settings.ELASTICSEARCH_URL,
        request_timeout=settings.ELASTICSEARCH_TIMEOUT,
        retry_on_timeout=True,
        max_retries=1,
    )


def extract_category_and_store(notification: Notification) -> tuple[str, Optional[int], Optional[str]]:
    store = notification.stores.order_by("pk").first()
    return notification.category, store.pk if store else None, store.name if store else None


def notification_document(notification: Notification) -> dict[str, Any]:
    category, store_id, store_name = extract_category_and_store(notification)
    return {
        "id": notification.id,
        "recipient_id": notification.recipient_id,
        "verb": notification.verb,
        "description": notification.description or "",
        "level": notification.level,
        "category": category,
        "store_id": store_id,
        "store_ids": list(notification.stores.values_list("pk", flat=True)),
        "store_name": store_name or "",
        "unread": notification.unread,
        "deleted": notification.deleted,
        "public": notification.public,
        "timestamp": notification.timestamp.isoformat() if notification.timestamp else None,
    }


def ensure_notifications_index(es: Any = None) -> bool:
    if not enabled():
        return False
    from elasticsearch.exceptions import ApiError, BadRequestError

    es = es or client()
    index = getattr(
        settings,
        "ELASTICSEARCH_NOTIFICATIONS_INDEX",
        f"{getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'commerce')}-notifications",
    )
    try:
        if es.indices.exists(index=index):
            return True
        es.indices.create(
            index=index,
            mappings={
                "properties": {
                    "id": {"type": "integer"},
                    "recipient_id": {"type": "integer"},
                    "verb": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "description": {"type": "text"},
                    "level": {"type": "keyword"},
                    "category": {"type": "keyword"},
                    "store_id": {"type": "integer"},
                    "store_ids": {"type": "integer"},
                    "store_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "unread": {"type": "boolean"},
                    "deleted": {"type": "boolean"},
                    "public": {"type": "boolean"},
                    "timestamp": {"type": "date"},
                }
            },
        )
        return True
    except (BadRequestError, ApiError) as exc:
        if "resource_already_exists_exception" in str(exc) or getattr(exc, "status_code", None) == 400:
            return True
        raise


def index_notification(notification_id: int, *, strict: bool = False, es: Any = None) -> None:
    if not enabled():
        return
    try:
        notification = Notification.objects.select_related(
            "recipient",
            "actor_content_type",
            "target_content_type",
            "action_object_content_type",
        ).get(pk=notification_id)
        active_es = es or client()
        ensure_notifications_index(active_es)
        index = getattr(
            settings,
            "ELASTICSEARCH_NOTIFICATIONS_INDEX",
            f"{getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'commerce')}-notifications",
        )
        active_es.index(
            index=index,
            id=notification.id,
            document=notification_document(notification),
        )
    except Notification.DoesNotExist:
        delete_notification(notification_id, strict=strict, es=es)
    except Exception:
        logger.exception("Failed to index notification in Elasticsearch", extra={"notification_id": notification_id})
        if strict:
            raise


def delete_notification(notification_id: int, *, strict: bool = False, es: Any = None) -> None:
    if not enabled():
        return
    try:
        index = getattr(
            settings,
            "ELASTICSEARCH_NOTIFICATIONS_INDEX",
            f"{getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'commerce')}-notifications",
        )
        active_es = es or client()
        active_es.delete(
            index=index,
            id=notification_id,
            ignore=[404],
        )
    except Exception:
        logger.exception("Failed to delete notification from Elasticsearch", extra={"notification_id": notification_id})
        if strict:
            raise


def _schedule_notification(notification_id: int, *, deleted: bool = False, recipient_id: int | None = None) -> None:
    if not enabled():
        return
    if recipient_id is None:
        recipient_id = Notification.objects.filter(pk=notification_id).values_list("recipient_id", flat=True).first()
    SearchIndexJob.objects.update_or_create(
        action=SearchIndexJob.SYNC_NOTIFICATION, target_id=notification_id,
        defaults={"revision": uuid.uuid4(), "recipient_id": recipient_id},
    )

    def enqueue() -> None:
        from notification.tasks import index_notification_task, delete_notification_task
        task = delete_notification_task if deleted else index_notification_task
        try:
            task.apply_async(args=(notification_id,), retry=False)
        except Exception:
            logger.exception("Unable to enqueue notification search reconciliation", extra={"notification_id": notification_id})
    transaction.on_commit(enqueue)


def schedule_index_notification(notification_id: int) -> None:
    _schedule_notification(notification_id)


def schedule_delete_notification(notification_id: int, *, recipient_id: int | None = None) -> None:
    _schedule_notification(notification_id, deleted=True, recipient_id=recipient_id)


def search_notification_ids(
    query: str,
    recipient_id: int,
    filters: Optional[dict[str, Any]] = None,
    offset: int = 0,
    limit: int = 20,
    es: Any = None,
) -> Optional[dict[str, Any]]:
    if not enabled() or not query:
        return None
    if SearchIndexJob.objects.filter(
        action=SearchIndexJob.SYNC_NOTIFICATION, recipient_id=recipient_id,
    ).exists():
        return None
    filters = filters or {}
    must = [{
        "multi_match": {
            "query": query,
            "fields": ["verb^4", "description^2", "store_name^2", "category"],
            "fuzziness": "AUTO",
        }
    }]

    filter_clauses = [
        {"term": {"recipient_id": recipient_id}},
    ]

    include_archived = filters.get("include_archived", False)
    if not include_archived:
        filter_clauses.append({"term": {"deleted": False}})

    state = filters.get("state")
    if state == "unread":
        filter_clauses.append({"term": {"unread": True}})
    elif state == "read":
        filter_clauses.append({"term": {"unread": False}})

    category = filters.get("category")
    if category and category != "all":
        filter_clauses.append({"term": {"category": category}})

    store_id = filters.get("store_id")
    if store_id is not None:
        filter_clauses.append({"term": {"store_ids": int(store_id)}})

    level = filters.get("level")
    if level:
        filter_clauses.append({"term": {"level": level}})

    index = getattr(
        settings,
        "ELASTICSEARCH_NOTIFICATIONS_INDEX",
        f"{getattr(settings, 'ELASTICSEARCH_INDEX_PREFIX', 'commerce')}-notifications",
    )

    try:
        active_es = es or client()
        response = active_es.search(
            index=index,
            query={"bool": {"must": must, "filter": filter_clauses}},
            sort=[{"timestamp": "desc"}, {"id": "desc"}],
            from_=offset,
            size=limit,
            track_total_hits=True,
        )
    except Exception:
        logger.exception("Elasticsearch notification search failed")
        return None

    hits = response["hits"]["hits"]
    total = response["hits"]["total"]
    if isinstance(total, dict):
        total = total["value"]
    return {
        "ids": [int(hit["_id"]) for hit in hits],
        "count": int(total),
    }
