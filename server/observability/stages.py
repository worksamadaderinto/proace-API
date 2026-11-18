"""Request-local, bounded stage timings without query text or business payloads."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from math import isfinite

from .tracing import tracer

request_stages: ContextVar[dict[str, float] | None] = ContextVar(
    "request_stages", default=None
)
STAGES = frozenset({"catalog_cache", "search", "hydrate", "serialize", "search_engine", "search_fallback", "catalog_count", "catalog_page_ids", "catalog_hydration", "search_server"})


@contextmanager
def stage(name: str) -> Iterator[None]:
    if name not in STAGES:
        raise ValueError("Unknown request timing stage")
    timings = request_stages.get()
    if timings is None:
        yield
        return
    started = perf_counter()
    try:
        with tracer().start_as_current_span(f"catalog.{name}"):
            yield
    finally:
        timings[name] = timings.get(name, 0.0) + (perf_counter() - started) * 1000


def observe_stage(name: str, milliseconds: float) -> None:
    """Record bounded upstream timing metadata, never provider/query payloads."""
    if name not in STAGES:
        raise ValueError("Unknown request timing stage")
    timings = request_stages.get()
    if timings is not None and isfinite(milliseconds) and milliseconds >= 0:
        timings[name] = milliseconds
