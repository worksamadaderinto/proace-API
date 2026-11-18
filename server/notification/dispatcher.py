from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


@runtime_checkable
class NotificationDispatcher(Protocol):
    """Abstract interface for user notification and alert dispatching."""

    def notify(
        self,
        recipient: Any,
        verb: str,
        *,
        actor: Any = None,
        target: Any = None,
        action_object: Any = None,
        description: str = "",
        level: str = "info",
        data: dict[str, Any] | None = None,
        public: bool = True,
        event_key: str | None = None,
    ) -> Any:
        """Dispatch a notification to the specified recipient."""
        ...


class DatabaseNotificationDispatcher:
    """Production notification adapter creating persistent Notification records."""

    def notify(
        self,
        recipient: Any,
        verb: str,
        *,
        actor: Any = None,
        target: Any = None,
        action_object: Any = None,
        description: str = "",
        level: str = "info",
        data: dict[str, Any] | None = None,
        public: bool = True,
        event_key: str | None = None,
    ) -> Any:
        from notification.views import create_notification

        return create_notification(
            recipient=recipient,
            verb=verb,
            actor=actor,
            target=target,
            action_object=action_object,
            description=description,
            level=level,
            data=data,
            public=public,
            event_key=event_key,
        )


class MockNotificationDispatcher:
    """Deterministic in-memory notification recorder for testing and offline execution."""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    def notify(
        self,
        recipient: Any,
        verb: str,
        *,
        actor: Any = None,
        target: Any = None,
        action_object: Any = None,
        description: str = "",
        level: str = "info",
        data: dict[str, Any] | None = None,
        public: bool = True,
        event_key: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "recipient": recipient,
            "verb": verb,
            "actor": actor,
            "target": target,
            "action_object": action_object,
            "description": description,
            "level": level,
            "data": data or {},
            "public": public,
            "event_key": event_key,
        }
        self.dispatched.append(entry)
        return entry


def get_notification_dispatcher() -> NotificationDispatcher:
    """Resolve and instantiate the configured notification dispatcher from settings."""
    backend_path = getattr(
        settings,
        "NOTIFICATION_DISPATCHER_BACKEND",
        "notification.dispatcher.DatabaseNotificationDispatcher",
    )
    if isinstance(backend_path, str):
        try:
            backend_cls = import_string(backend_path)
            return backend_cls()
        except Exception as exc:
            logger.warning(
                "Could not load NOTIFICATION_DISPATCHER_BACKEND '%s': %s. Falling back to DatabaseNotificationDispatcher.",
                backend_path,
                exc,
            )
            return DatabaseNotificationDispatcher()
    return DatabaseNotificationDispatcher()
