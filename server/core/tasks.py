"""Background tasks for core authentication and account lifecycle."""
from __future__ import annotations

import logging
from celery import shared_task
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)


@shared_task(name="core.tasks.import_google_avatar_task", max_retries=2, default_retry_delay=5)
def import_google_avatar_task(user_id: int, picture: str) -> None:
    """Download, resize, and store a social avatar asynchronously in Celery."""
    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.warning("User %s not found for avatar import", user_id)
        return
    from core.social_avatar import import_google_avatar
    import_google_avatar(user, picture)
