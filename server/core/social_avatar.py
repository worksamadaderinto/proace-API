"""Import a verified Google profile photo through the configured media storage."""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from core.models import User

logger = logging.getLogger(__name__)
MAX_PHOTO_BYTES = 5 * 1024 * 1024


@runtime_checkable
class AvatarFetcher(Protocol):
    """Protocol defining avatar image network downloading."""

    def fetch(self, url: str, max_bytes: int = MAX_PHOTO_BYTES) -> bytes:
        """Download raw avatar image bytes, raising an error if unreachable or size exceeded."""
        ...


class RequestsAvatarFetcher:
    """Production avatar fetcher using requests streaming."""

    def fetch(self, url: str, max_bytes: int = MAX_PHOTO_BYTES) -> bytes:
        with requests.get(url, timeout=(3, 5), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                logger.warning('Avatar download rejected (status %s)', response.status_code)
                raise ValueError(f'Avatar download rejected with status {response.status_code}')
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ValueError('Avatar exceeds size limit')
            return bytes(content)


class MockAvatarFetcher:
    """Deterministic in-memory avatar fetcher for offline tests."""

    def __init__(self, content: bytes | None = None) -> None:
        self.content = content
        self.fetched_urls: list[str] = []

    def fetch(self, url: str, max_bytes: int = MAX_PHOTO_BYTES) -> bytes:
        self.fetched_urls.append(url)
        if self.content is not None:
            return self.content
        out = BytesIO()
        Image.new('RGB', (100, 100), 'blue').save(out, format='JPEG')
        return out.getvalue()


def get_avatar_fetcher() -> AvatarFetcher:
    """Resolve and instantiate the configured avatar fetcher backend from settings."""
    backend_path = getattr(settings, 'AVATAR_FETCHER_BACKEND', None)
    if backend_path:
        from django.utils.module_loading import import_string
        try:
            return import_string(backend_path)()
        except Exception as exc:
            logger.warning('Could not load AVATAR_FETCHER_BACKEND %s: %s', backend_path, exc)
    return RequestsAvatarFetcher()


def import_google_avatar(user: User, picture: str, fetcher: AvatarFetcher | None = None) -> None:
    """Fill a missing avatar without replacing a user-selected profile photo."""
    if user.avatar_image or user.avatar_url or not picture:
        return
    try:
        parsed = urlsplit(picture)
        port = parsed.port
    except ValueError:
        return
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or not host.endswith('.googleusercontent.com')
            or parsed.username or parsed.password or port not in (None, 443)):
        return
    try:
        fetcher_inst = fetcher or get_avatar_fetcher()
        raw_bytes = fetcher_inst.fetch(picture, MAX_PHOTO_BYTES)
        with Image.open(BytesIO(raw_bytes)) as image:
            if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > 16000000:
                raise ValueError('Unsupported avatar image')
            image = ImageOps.fit(ImageOps.exif_transpose(image).convert('RGB'), (512, 512))
            output = BytesIO()
            image.save(output, format='JPEG', quality=90)
        user.avatar_image.save(f'google-{uuid4().hex}.jpg', ContentFile(output.getvalue()), save=False)
        user.avatar_url = ''
        user.save(update_fields=['avatar_image', 'avatar_url', 'updated'])
    except (requests.RequestException, UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        logger.warning('Google avatar import unavailable for user %s (%s)', user.pk, type(error).__name__)
    except Exception as error:
        # Storage providers may raise their own errors; photo failure must not block login.
        logger.error('Google avatar storage failed for user %s (%s)', user.pk, type(error).__name__)


def schedule_import_google_avatar(user: User, picture: str, dispatcher: Any = None) -> None:
    """Schedule avatar download and processing off the main request thread."""
    if user.avatar_image or user.avatar_url or not picture:
        return
    from django.conf import settings
    from core.tasks import import_google_avatar_task
    from utils.task_dispatcher import get_task_dispatcher

    active_dispatcher = dispatcher or get_task_dispatcher()
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        active_dispatcher.dispatch(import_google_avatar_task, user.pk, picture)
        user.refresh_from_db()
    else:
        from django.db import transaction

        transaction.on_commit(lambda: active_dispatcher.dispatch(import_google_avatar_task, user.pk, picture))
