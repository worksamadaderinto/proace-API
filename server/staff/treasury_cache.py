"""Revision-scoped dashboard reports; never used to authorize withdrawals."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from django.core.cache import cache
from django.db import connection

if TYPE_CHECKING:
    from .views import TreasuryEarnings

REVISION_KEY = "backoffice:treasury:revision"
REPORT_TTL_SECONDS = 60
Report = tuple["TreasuryEarnings", "TreasuryEarnings"]


def invalidate_report() -> None:
    cache.set(REVISION_KEY, uuid4().hex, timeout=None)


def invalidate_summary(instance: Any = None) -> None:
    try:
        from .models import TreasuryReportSummary

        qs = TreasuryReportSummary.objects.filter(is_valid=True)
        if instance is not None and getattr(instance, "created", None):
            qs = qs.filter(reconciled_through__gt=instance.created)
        qs.update(is_valid=False)
    except Exception:
        pass


def read_report(build: Callable[[], Report]) -> Report:
    # A transaction may see uncommitted writes. Never publish that snapshot.
    if connection.in_atomic_block:
        return build()
    revision = cache.get_or_set(REVISION_KEY, uuid4().hex, timeout=None)
    if not isinstance(revision, str):
        return build()
    key = f"backoffice:treasury:report:{revision}"
    report = cache.get(key)
    if report is not None:
        return report
    report = build()
    # A racing write rotates the revision. Never attach old totals to a new one.
    if cache.get(REVISION_KEY) == revision:
        cache.set(key, report, timeout=REPORT_TTL_SECONDS)
    return report


