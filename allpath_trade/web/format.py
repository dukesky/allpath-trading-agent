from __future__ import annotations

import re
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


_HORIZON_LABELS = {"long": "Long-term", "medium": "Medium-term", "swing": "Swing"}


def horizon_label(value: str | None) -> str:
    """Display text for a StrategyHorizon value on the strategies page's
    chip. `value` is unset -> "" (no chip rendered) rather than a guessed
    label -- see StrategyDoc.horizon's own docstring."""
    if value is None:
        return ""
    return _HORIZON_LABELS.get(value, value)


_THESIS_EXCERPT_LIMIT = 140
# A period/!/? followed by whitespace-or-end-of-string, or a bare newline
# (a YAML block-scalar thesis often wraps onto a second line before ever
# hitting terminal punctuation) -- either one marks "the first sentence is
# over" for this excerpt's purposes.
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)|\n")


def thesis_excerpt(thesis: str | None, limit: int = _THESIS_EXCERPT_LIMIT) -> str:
    """First sentence of a strategy's thesis (the user's own words on the
    outlook), capped at `limit` characters. Pure text slicing -- Jinja's
    autoescape (on by default for .html templates) is what actually makes
    this HTML-safe on render; this function only decides which characters
    to show."""
    text = (thesis or "").strip()
    if not text:
        return ""
    match = _SENTENCE_END_RE.search(text)
    first = text[:match.end()].strip() if match else text
    if len(first) > limit:
        first = first[:limit].rstrip() + "…"
    return first


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
