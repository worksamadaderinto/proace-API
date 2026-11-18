"""Validated, read-only catalog fixtures shared by performance runners."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx


async def discover_catalog(client: httpx.AsyncClient, base_url: str) -> dict[str, Any]:
    response = await client.get(f"{base_url}/api/v1/products/")
    response.raise_for_status()
    data = response.json()
    items = data.get("results", [])
    if not items:
        raise ValueError(
            "Catalog benchmark requires visible products; no data was changed."
        )
    categories = sorted({item["category"] for item in items if item.get("category")})
    if not categories:
        raise ValueError("Catalog benchmark requires products with categories.")
    return {
        "ids": [str(item["id"]) for item in items],
        "categories": categories,
        "search": items[0]["title"],
        "pages": int(data["pages"]),
        "count": int(data["count"]),
    }


def catalog_path(**params: Any) -> str:
    return "/api/v1/products/?" + urlencode(params)


def result_count(response: httpx.Response) -> int:
    if response.status_code != 200:
        return 0
    payload = response.json()
    return len(payload.get("results", []))


async def route_api_request(request: httpx.Request) -> None:
    """Allow identical workloads through Next.js with PERFORMANCE_API_PREFIX=/api."""
    import os

    prefix = os.environ.get("PERFORMANCE_API_PREFIX", "/api/v1").rstrip("/")
    if not prefix.startswith("/"):
        raise ValueError("PERFORMANCE_API_PREFIX must start with /")
    view = os.environ.get("PERFORMANCE_CATALOG_VIEW", "full")
    if view not in {"full", "card"}:
        raise ValueError("PERFORMANCE_CATALOG_VIEW must be full or card")
    if request.url.path.rstrip("/") == "/api/v1/products" and view == "card":
        request.url = request.url.copy_merge_params({"view": view})
    if request.url.path.startswith("/api/v1/"):
        path = prefix + request.url.path[len("/api/v1") :]
        if prefix == "/api":
            path = path.rstrip("/")
        request.url = request.url.copy_with(path=path)


def server_timings(header: str) -> dict[str, float]:
    """Keep finite bounded duration metrics; discard descriptions and unknown data."""
    import math
    allowed = {"app", "db", "catalog_cache", "search", "hydrate", "serialize", "search_engine", "search_fallback", "catalog_count", "catalog_page_ids", "catalog_hydration", "search_server"}
    result: dict[str, float] = {}
    for metric in header[:4096].split(","):
        name, *parameters = metric.strip().split(";")
        if name not in allowed:
            continue
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if key != "dur" or not separator:
                continue
            try:
                duration = float(value)
            except ValueError:
                continue
            if math.isfinite(duration) and duration >= 0:
                result[name] = duration
    return result
