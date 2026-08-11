from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

# Re-exported for the `is_recent_submission` Jinja filter (see templating.py)
# -- dashboard.html's recent-trades row needs the same "is this row still
# within the honest fill-pending window" check that agent/readonly_tools.py's
# _format_recent_trade uses (I2), and store/journal.py is the shared home for
# it (see that module's docstring on FILL_PENDING_WINDOW_HOURS) since both
# `web` and `agent` already depend on it for TradeJournal.
from allpath_trade.store.journal import is_recent_submission  # noqa: F401


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
# A period/!/? followed by whitespace-or-end-of-string marks "the first
# sentence is over". A bare newline is deliberately NOT treated as a
# sentence boundary: real theses come from YAML block scalars hard-wrapped
# at ~80 chars, so the first newline routinely lands mid-sentence (e.g.
# "...with extreme\nprice swings...") rather than at its end -- treating it
# as a boundary truncated real theses mid-clause with no ellipsis to even
# signal the cut. The newline is still used, but only as a last-resort
# fallback below, when there is no terminal punctuation anywhere in the text.
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
# A match before this many characters is treated as untrustworthy rather
# than a real sentence end -- pragmatic (not a real abbreviation detector)
# guard against theses that open with something like "U.S. stocks..." or
# "Q3 EPS...", where the first period isn't the end of the first sentence.
# A genuinely short first sentence (< 20 chars) pays for this by being
# skipped in favor of the next one; that's an acceptable trade-off for a
# display-only excerpt.
_MIN_SENTENCE_CHARS = 20


def thesis_excerpt(thesis: str | None, limit: int = _THESIS_EXCERPT_LIMIT) -> str:
    """First sentence of a strategy's thesis (the user's own words on the
    outlook), capped at `limit` characters, with a trailing "…" appended
    whenever the shown text is shorter than the full thesis (whichever of
    the cases below caused the cut). Pure text slicing -- Jinja's
    autoescape (on by default for .html templates) is what actually makes
    this HTML-safe on render; this function only decides which characters
    to show."""
    text = (thesis or "").strip()
    if not text:
        return ""

    match = next(
        (m for m in _SENTENCE_END_RE.finditer(text) if m.start() >= _MIN_SENTENCE_CHARS),
        None,
    )
    if match:
        first = text[:match.end()].strip()
    elif not _SENTENCE_END_RE.search(text):
        # No sentence-ending punctuation anywhere -- e.g. a thesis written
        # entirely in CJK, which uses '。' and won't match the ASCII regex
        # above (that case just falls through to the length cap below).
        # Only now, once a real sentence boundary is ruled out, fall back to
        # the first hard-wrap newline.
        newline = text.find("\n")
        first = text[:newline].strip() if newline != -1 else text
    else:
        # Sentence-ending punctuation exists, but every occurrence sits
        # inside the first _MIN_SENTENCE_CHARS characters (an
        # abbreviation-only opening) -- show the whole thesis and let the
        # length cap below trim it if needed, rather than trusting an
        # untrustworthy early match.
        first = text

    if len(first) > limit:
        first = first[:limit].rstrip() + "…"
    elif len(first) < len(text):
        first = first + "…"
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
