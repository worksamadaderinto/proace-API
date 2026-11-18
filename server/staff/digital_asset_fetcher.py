"""
Dependency Injection Gateway for Digital Asset Remote Retrieval.

Decouples bounded HTTP digital product asset streaming from socket handlers,
allowing deterministic mocking and testing without opening outbound sockets.
"""

from __future__ import annotations

import importlib
import logging
import tempfile
import urllib.request
from io import BytesIO
from typing import Any, BinaryIO, Protocol, runtime_checkable

from django.conf import settings

logger = logging.getLogger(__name__)


@runtime_checkable
class DigitalAssetFetcher(Protocol):
    """Protocol for safely fetching remote digital assets into a temporary file or buffer."""

    def fetch_asset(self, file_url: str, max_size: int = 50 * 1024 * 1024) -> BinaryIO:
        """Fetch remote asset stream and return a seeked readable binary file."""
        ...


class HttpDigitalAssetFetcher:
    """Production asset fetcher with pinned IP and SSRF prevention handlers."""

    def fetch_asset(self, file_url: str, max_size: int = 50 * 1024 * 1024) -> BinaryIO:
        from staff.digital_assets import (
            PublicHTTPHandler,
            PublicHTTPSHandler,
            PublicRedirectHandler,
        )

        req = urllib.request.Request(
            file_url,
            headers={"User-Agent": "ProAce-Backoffice-Audit/1.0"},
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            PublicHTTPHandler(),
            PublicHTTPSHandler(),
            PublicRedirectHandler(),
        )
        asset = tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024)
        total_bytes = 0
        try:
            with opener.open(req, timeout=15) as conn:
                while True:
                    chunk = conn.read(64 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_size:
                        raise ValueError(f"Digital asset exceeded maximum allowed size ({max_size} bytes).")
                    asset.write(chunk)
            asset.seek(0)
            return asset
        except Exception:
            asset.close()
            raise


class MockDigitalAssetFetcher:
    """Deterministic in-memory fetcher returning predefined byte buffers."""

    def __init__(self, content: bytes | None = None, error: Exception | None = None) -> None:
        self.content = content if content is not None else b"%PDF-1.4 mock digital content"
        self.error = error
        self.fetched_urls: list[str] = []

    def fetch_asset(self, file_url: str, max_size: int = 50 * 1024 * 1024) -> BinaryIO:
        self.fetched_urls.append(file_url)
        if self.error:
            raise self.error
        if len(self.content) > max_size:
            raise ValueError(f"Digital asset exceeded maximum allowed size ({max_size} bytes).")
        buffer = BytesIO(self.content)
        return buffer


def get_digital_asset_fetcher() -> DigitalAssetFetcher:
    """Factory to retrieve the active digital asset fetcher configured in settings."""
    backend_path = getattr(
        settings,
        "DIGITAL_ASSET_FETCHER_BACKEND",
        "staff.digital_asset_fetcher.HttpDigitalAssetFetcher",
    )
    module_path, class_name = backend_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls()
