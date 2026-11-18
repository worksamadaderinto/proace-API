"""
Dependency Injection Gateway for Mobile & Browser Push Notifications (FCM).

Decouples push delivery batches from the concrete Firebase Admin SDK, enabling
offline unit tests, hermetic integration test runs, and pluggable push transport backends.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class PushTokenResult:
    """Result for an individual device token in a multicast push batch."""

    success: bool
    exception: Any = None


@dataclass
class PushBatchResult:
    """Aggregated result of a multicast push delivery attempt."""

    success_count: int
    responses: list[PushTokenResult] = field(default_factory=list)


@runtime_checkable
class PushGateway(Protocol):
    """Protocol defining push notification delivery operations."""

    def send_multicast(
        self,
        tokens: list[str],
        data: dict[str, str],
        title: str | None = None,
        body: str | None = None,
        is_native: bool = False,
        app: Any = None,
    ) -> PushBatchResult:
        """Send a multicast push notification to multiple device tokens."""
        ...


class FirebasePushGateway:
    """Production push gateway delegating to firebase_admin.messaging."""

    def send_multicast(
        self,
        tokens: list[str],
        data: dict[str, str],
        title: str | None = None,
        body: str | None = None,
        is_native: bool = False,
        app: Any = None,
    ) -> PushBatchResult:
        from firebase_admin import messaging

        display = None
        if is_native and (title or body):
            display = messaging.Notification(title=title or "", body=body or "")

        multicast_message = messaging.MulticastMessage(
            data=data,
            tokens=tokens,
            notification=display,
        )
        response = messaging.send_each_for_multicast(multicast_message, app=app)

        mapped_responses = [
            PushTokenResult(
                success=r.success,
                exception=getattr(r, "exception", None),
            )
            for r in getattr(response, "responses", [])
        ]
        return PushBatchResult(
            success_count=getattr(response, "success_count", len(tokens)),
            responses=mapped_responses,
        )


class MockPushGateway:
    """In-memory deterministic push gateway for testing without Firebase credentials."""

    def __init__(self, should_fail_tokens: set[str] | None = None) -> None:
        self.should_fail_tokens = should_fail_tokens or set()
        self.batches: list[dict[str, Any]] = []

    def send_multicast(
        self,
        tokens: list[str],
        data: dict[str, str],
        title: str | None = None,
        body: str | None = None,
        is_native: bool = False,
        app: Any = None,
    ) -> PushBatchResult:
        self.batches.append({
            "tokens": list(tokens),
            "data": dict(data),
            "title": title,
            "body": body,
            "is_native": is_native,
            "app": app,
        })
        responses = []
        success_count = 0
        for token in tokens:
            if token in self.should_fail_tokens:
                class MockTokenException(Exception):
                    code = "messaging/invalid-registration-token"
                responses.append(PushTokenResult(success=False, exception=MockTokenException("Invalid token")))
            else:
                responses.append(PushTokenResult(success=True))
                success_count += 1
        return PushBatchResult(success_count=success_count, responses=responses)


def get_push_gateway() -> PushGateway:
    """Factory to retrieve the active push gateway configured in settings."""
    backend_path = getattr(
        settings,
        "PUSH_GATEWAY_BACKEND",
        "notification.push_gateway.FirebasePushGateway",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
