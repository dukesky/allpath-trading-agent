from __future__ import annotations

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.memory.guard import MemoryGuardError, scan_entry
from allpath_trade.store.views import MAX_VIEW_DAYS, ViewStore


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

    def record_view(subject: str, stance: str, horizon: str, conviction: str,
                    invalidation: str, expires_days: int | None = None) -> str:
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
        return (f"View #{vid} recorded for {subject.strip().upper()}. Shadow's "
                "nightly reflection will have to say whether each revision "
                "agrees with, extends or contradicts it.")

    def list_views() -> str:
        rows = views.active()
        return "\n".join(format_view(v) for v in rows) or "no active views"

    t = "string"
    registry.register(
        "record_view",
        "File one of the owner's views in the views ledger: what he thinks "
        "about a ticker, a theme, or the market as a whole (subject MARKET). "
        "Record HIS position as he states it, even after you pushed back; "
        "your disagreement belongs in your reply, not in the view.",
        {"type": "object", "properties": {
            "subject": {"type": t, "description": "ticker (e.g. NVDA), a theme, or MARKET"},
            "stance": {"type": t, "description": "his view in one or two sentences"},
            "horizon": {"type": t, "description": "e.g. '2 weeks', '6-12 months'"},
            "conviction": {"type": t, "description": "low / medium / high"},
            "invalidation": {"type": t, "description": "what would change his mind"},
            "expires_days": {"type": "integer",
                             "description": f"days until the view lapses (default 30, max {MAX_VIEW_DAYS})"}},
         "required": ["subject", "stance", "horizon", "conviction", "invalidation"]},
        record_view)
    registry.register(
        "list_views",
        "List the owner's active views (unexpired), newest last.",
        {"type": "object", "properties": {}},
        list_views)
