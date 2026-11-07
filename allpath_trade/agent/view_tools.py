from __future__ import annotations

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.memory.guard import MemoryGuardError, scan_entry
from allpath_trade.store.views import DEFAULT_VIEW_DAYS, MAX_VIEW_DAYS, ViewStore

# What an unstated optional field is filed as: a bare directive ("drop the
# CRDO action") names no horizon or invalidation, and requiring them kept
# the agent from filing at all (2026-11-07).
NOT_STATED = "not stated"


def format_view(v: dict) -> str:
    return (f"#{v['id']} {v['subject']} ({v['ts'][:10]}, conviction "
            f"{v['conviction']}, horizon {v['horizon']}, expires "
            f"{v['expires_ts'][:10]}): {v['stance']} | invalidation: "
            f"{v['invalidation']}")


def register_view_tools(registry: ToolRegistry, *, views: ViewStore) -> None:
    """The owner's views ledger (shadow check-in pilot, spec
    2026-11-06-shadow-checkin-design.md). Registered only on shadow's chat
    agent, and only with SHADOW_DAILY_CHECKIN on. Every field goes through
    the same injection scan as curated memory: views are typed by the owner
    but filed by the agent, and reflection reads them back as context."""

    def record_view(subject: str, stance: str, horizon: str = NOT_STATED,
                    conviction: str = NOT_STATED, invalidation: str = NOT_STATED,
                    expires_days: int | None = None) -> str:
        horizon, conviction, invalidation = (
            (v or "").strip() or NOT_STATED for v in (horizon, conviction, invalidation))
        fields = (subject, stance, horizon, conviction, invalidation)
        try:
            for text in fields:
                scan_entry(text or "")
        except MemoryGuardError as exc:
            return f"error: {exc}"
        try:
            vid = views.add(subject=subject, stance=stance, horizon=horizon,
                            conviction=conviction, invalidation=invalidation,
                            expires_days=expires_days)
        except ValueError as exc:
            return f"error: {exc}"
        days = DEFAULT_VIEW_DAYS if expires_days is None else expires_days
        days = max(1, min(int(days), MAX_VIEW_DAYS))
        return (f"Filed as view #{vid}: {subject.strip().upper()} -- "
                f"{stance.strip()}, {days} days. Confirm it to him in one line "
                "in this form. Shadow's nightly reflection must now say whether "
                "each revision agrees with, extends or contradicts it.")

    def list_views() -> str:
        rows = views.active()
        return "\n".join(format_view(v) for v in rows) or "no active views"

    t = "string"
    registry.register(
        "record_view",
        "File one of the owner's views in the views ledger: what he thinks "
        "or wants about a ticker, a rule, a theme, or the book as a whole "
        "(subject MARKET). Directives count: 'keep', 'drop the action', "
        "'hold, no change', 'cap', 'exit', 'stay for now'. Call it once per "
        "subject. Record HIS position as he states it, even after you pushed "
        "back; your disagreement belongs in your reply, not in the view. "
        "Only subject and stance are required; leave out what he did not say.",
        {"type": "object", "properties": {
            "subject": {"type": t, "description": "ticker (e.g. NVDA), a theme, or MARKET"},
            "stance": {"type": t, "description": "his view in one or two sentences"},
            "horizon": {"type": t, "description": "e.g. '2 weeks', '6-12 months' (omit if not stated)"},
            "conviction": {"type": t, "description": "low / medium / high (omit if not stated)"},
            "invalidation": {"type": t, "description": "what would change his mind (omit if not stated)"},
            "expires_days": {"type": "integer",
                             "description": f"days until the view lapses (default 30, max {MAX_VIEW_DAYS})"}},
         "required": ["subject", "stance"]},
        record_view)
    registry.register(
        "list_views",
        "List the owner's active views (unexpired), newest last.",
        {"type": "object", "properties": {}},
        list_views)
