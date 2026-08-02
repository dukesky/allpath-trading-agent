from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal


def money(value) -> str:
    try:
        return f"${Decimal(str(value)):,.2f}"
    except Exception:  # noqa: BLE001 — templates must never raise
        return "—"


def pct(value) -> str:
    try:
        return f"{Decimal(str(value)) * 100:+.2f}%"
    except Exception:  # noqa: BLE001
        return "—"


def ago(ts: str) -> str:
    try:
        then = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ts or ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
