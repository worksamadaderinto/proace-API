"""
Dependency Injection Client for Local & External Computer Vision Services.

Decouples visual search and image embedding inference from the concrete HTTP client,
enabling offline testing, deterministic mocking, and fast local test suites.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Protocol, runtime_checkable

from django.conf import settings

logger = logging.getLogger(__name__)


@runtime_checkable
class VisionClient(Protocol):
    """Protocol for vision model inference HTTP/RPC requests."""

    def post(self, url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Send an inference request to the model service."""
        ...


class HttpVisionClient:
    """Production vision client delegating to requests.post."""

    def post(self, url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        import requests

        clean_url = str(url).rstrip("/")
        response = requests.post(f"{clean_url}/{path}", timeout=(2, 15), **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid model response")
        return payload


class MockVisionClient:
    """Deterministic mock client for unit testing vision pipelines."""

    def __init__(self, response_payload: dict[str, Any] | None = None) -> None:
        self.response_payload = response_payload or {}
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"url": url, "path": path, "kwargs": kwargs})
        if isinstance(self.response_payload, Exception):
            raise self.response_payload
        return self.response_payload


def get_vision_client() -> VisionClient:
    """Factory to retrieve the active vision client configured in settings."""
    backend_path = getattr(
        settings,
        "VISION_CLIENT_BACKEND",
        "product.vision_client.HttpVisionClient",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
