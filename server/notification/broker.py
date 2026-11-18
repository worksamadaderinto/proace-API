"""
Dependency Injection Broker for Realtime Server-Sent Events & PubSub.

Decouples SSE / live WebSocket pubsub messaging from the concrete Redis client,
allowing deterministic in-memory testing without a live Redis server.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Protocol, runtime_checkable

from django.conf import settings

logger = logging.getLogger(__name__)


@runtime_checkable
class RealtimeBroker(Protocol):
    """Protocol for publish/subscribe event routing across processes."""

    def publish(self, channel: str, message: str) -> int:
        """Publish an event string to a channel."""
        ...

    def pubsub(self, ignore_subscribe_messages: bool = True) -> Any:
        """Create a pubsub subscription instance."""
        ...

    def close(self) -> None:
        """Close connection resources."""
        ...


class RedisRealtimeBroker:
    """Production realtime broker delegating to Redis."""

    def __init__(self, url: str | None = None) -> None:
        self.url = (
            url
            or getattr(settings, "REALTIME_REDIS_URL", None)
            or getattr(settings, "CACHE_URL", None)
            or "redis://127.0.0.1:6379/0"
        )
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from redis import Redis

            self._client = Redis.from_url(
                self.url,
                socket_connect_timeout=2,
                socket_timeout=3,
                decode_responses=True,
            )
        return self._client

    def publish(self, channel: str, message: str) -> int:
        return self._get_client().publish(channel, message)

    def pubsub(self, ignore_subscribe_messages: bool = True) -> Any:
        return self._get_client().pubsub(ignore_subscribe_messages=ignore_subscribe_messages)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


class MockPubSub:
    """Mock pubsub subscription for testing SSE streams."""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.subscribed_channels: list[str] = []
        self._messages = list(messages or [])
        self.closed = False

    def subscribe(self, channel: str) -> None:
        self.subscribed_channels.append(channel)

    def listen(self) -> Any:
        for msg in self._messages:
            yield msg

    def close(self) -> None:
        self.closed = True


class MockRealtimeBroker:
    """In-memory deterministic event broker for testing."""

    def __init__(self, mock_messages: list[dict[str, Any]] | None = None) -> None:
        self.published_events: list[tuple[str, str]] = []
        self.mock_messages = mock_messages or []
        self.active_pubsubs: list[MockPubSub] = []
        self.closed = False

    def publish(self, channel: str, message: str) -> int:
        self.published_events.append((channel, message))
        return 1

    def pubsub(self, ignore_subscribe_messages: bool = True) -> MockPubSub:
        sub = MockPubSub(self.mock_messages)
        self.active_pubsubs.append(sub)
        return sub

    def close(self) -> None:
        self.closed = True


def get_realtime_broker() -> RealtimeBroker:
    """Factory to retrieve the active realtime broker configured in settings."""
    backend_path = getattr(
        settings,
        "REALTIME_BROKER_BACKEND",
        "notification.broker.RedisRealtimeBroker",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
