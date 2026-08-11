from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from allpath_trade.agent.compact import Compactor
from allpath_trade.agent.context import build_system_prompt, load_identity
from allpath_trade.agent.loop import AgentSession
from allpath_trade.agent.memory_tools import register_memory_tools
from allpath_trade.agent.readonly_tools import register_readonly_tools
from allpath_trade.agent.reflection_tools import register_reflection_tools
from allpath_trade.agent.tools import ToolRegistry, fence_external
from allpath_trade.config import Settings
from allpath_trade.llm.base import LLMClient
from allpath_trade.scheduler import ET
from allpath_trade.store.conversations import ConversationStore

# Seed-briefing hard caps (spec §②: "种子简报...全部 fence_external 围栏").
# Each is independent -- a chatty observation day can't starve the trades
# block, and vice versa -- and the per-block char cap is the last-resort
# backstop against any one block (e.g. a very long trade reason string)
# blowing past a sane prompt size on its own.
MAX_TRADES = 30
MAX_OBSERVATION_LINES = 50
MAX_BLOCK_CHARS = 2000

_TRUNCATION_NOTE = "\n... (truncated)"

# Exact corrective-retry text from the Task 4 brief -- the one nudge a
# reflection session gets to reproduce the REPORT/SUMMARY structure before
# the run is recorded as failed.
CORRECTIVE_PROMPT = ("Your last message must end with the REPORT/SUMMARY "
                     "structure. Reproduce it now, nothing else.")

REFLECTION_INSTRUCTIONS = """\
## Daily reflection

You are now running the end-of-day reflection pass, not a live chat with the
user. Nobody is watching this in real time -- the Reports page replays the
transcript afterward, and only a short push-notification summary goes out
immediately. You have no order tool and no way to apply anything yourself:
every conclusion you act on goes through memory_update (curated memory) or
propose_strategy_revision (queued for the user's approval on the Pending
page) -- you are advisory only.

A deterministic seed briefing appears in the first user message below:
today's trades, today's observations, today's position day-changes, and the
pending queue, each fenced as external content. Start from those known
facts; spend your tool calls investigating what looks off, not
re-deriving what the briefing already told you.

Your job for today:
1. Review the day against each active strategy's stated thesis and rules
   (list_strategies / read_strategy / get_bars / get_quote / web_search /
   get_portfolio / list_pending_reviews / session_search / memory_read are
   all available).
2. When you reach a durable conclusion worth remembering -- a lesson
   learned, a pattern confirmed or broken -- write it with memory_update.
   Only your own concise conclusions; never paste external content
   verbatim.
3. When a strategy's real-world behavior measurably diverges from its
   stated assumption, propose a fix with propose_strategy_revision,
   including your rationale. Do not propose a revision just because you
   can -- only when something is measurably off.

You have a limited number of tool calls for this pass -- budget them, and
leave enough turns to write the final report.

When you are done, your FINAL message must end with EXACTLY this structure
and nothing after it:

REPORT
<Day summary>
<Per-strategy check>
<Lessons>
<Proposals>
SUMMARY
<3-5 plain sentences suitable for a phone push notification>

The line containing only the word SUMMARY marks where the report ends and
the notification text begins -- both sections are required and must be
non-empty.
"""


def _et_date(now: datetime | None) -> str:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(ET).date().isoformat()


def _ts_to_et_date(ts_iso: str) -> str | None:
    try:
        dt = datetime.fromisoformat(ts_iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET).date().isoformat()


def _et_day_bounds_utc(et_date: str) -> tuple[str, str]:
    """UTC ISO bounds of the ET calendar day `et_date`, used only as a cheap
    SQL-level pre-filter (`ObservationLog.recent`'s `since_iso`) -- the exact
    per-row cut still happens with `_ts_to_et_date`."""
    d = date.fromisoformat(et_date)
    start_et = datetime.combine(d, time.min, tzinfo=ET)
    end_et = datetime.combine(d, time.max, tzinfo=ET)
    return start_et.astimezone(UTC).isoformat(), end_et.astimezone(UTC).isoformat()


def _parse_report(text: str) -> tuple[str, str] | None:
    """Split on the LAST line that is exactly "SUMMARY" (spec §②). Both the
    report body and the summary must be non-empty, or the transcript doesn't
    actually carry the required structure and this is a parse failure."""
    lines = text.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if line.strip() == "SUMMARY":
            idx = i
    if idx is None:
        return None
    body = "\n".join(lines[:idx]).strip()
    summary = "\n".join(lines[idx + 1:]).strip()
    if not body or not summary:
        return None
    return body, summary


def _cap_chars(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - len(_TRUNCATION_NOTE), 0)] + _TRUNCATION_NOTE


def _format_trade(t: dict) -> str:
    # Carry-forward from the Task 1 review: "submitted, fill pending" must
    # key off filled_avg_price IS NULL, not filled_qty -- TradeJournal.record
    # always writes filled_qty as a string ("0" included), so filled_qty is
    # never NULL even for a not-yet-filled order; filled_avg_price is the
    # column that's genuinely NULL until a fill is observed.
    size = t.get("qty") or t.get("notional") or "?"
    if t.get("filled_avg_price") is None:
        fill = "submitted, fill pending"
    else:
        fill = f"filled {t.get('filled_qty')} @ {t.get('filled_avg_price')}"
    return (f"{str(t.get('ts', ''))[:19]} {t.get('side')} {t.get('ticker')} "
            f"size={size} status={t.get('status')} {fill} "
            f"strategy={t.get('strategy_id') or '-'}")


def _format_observation(o: dict) -> str:
    subject = o.get("subject") or "-"
    return f"{str(o.get('ts', ''))[:19]} [{o.get('source')}/{subject}] {o.get('text')}"


def _format_position(p: dict) -> str:
    note = f" ({p['note']})" if p.get("note") else ""
    return (f"{p.get('ticker')} qty={p.get('qty')} "
            f"avg_entry={p.get('avg_entry_price', 'n/a')} "
            f"day_change={p.get('day_change', 'n/a')}{note}")


def build_briefing(*, et_date: str, trades: list[dict], observations: list[dict],
                   positions: list[dict], pending_counts: dict[str, int]) -> str:
    """Pure, deterministic seed briefing -- no LLM, no I/O. `trades` /
    `observations` / `positions` are already-fetched plain dicts (the
    Reflector does the DB/broker/data-source reads); this function only
    formats and caps them. Each block is `fence_external`-wrapped
    independently (spec §②: "全部 fence_external 围栏") since every value
    inside ultimately traces back to model-authored strings (trade `reason`,
    observation `text`) or a remote price feed -- data, not instructions."""
    trades_block = "\n".join(_format_trade(t) for t in trades[:MAX_TRADES])
    trades_block = trades_block or "no trades today"

    obs_block = "\n".join(
        _format_observation(o) for o in observations[:MAX_OBSERVATION_LINES])
    obs_block = obs_block or "no observations today"

    pos_block = "\n".join(_format_position(p) for p in positions)
    pos_block = pos_block or "no open positions"

    pending_block = "\n".join(
        f"{kind}: {count}" for kind, count in sorted(pending_counts.items()))
    pending_block = pending_block or "no pending items"

    return "\n".join([
        f"# Daily reflection seed briefing -- {et_date}",
        "\n## Today's trades",
        fence_external(_cap_chars(trades_block)),
        "\n## Today's observations",
        fence_external(_cap_chars(obs_block)),
        "\n## Positions (day change)",
        fence_external(_cap_chars(pos_block)),
        "\n## Pending queue (by kind)",
        fence_external(_cap_chars(pending_block)),
    ])


@dataclass(kw_only=True)
class Reflector:
    """Runs the after-close daily reflection: the full-capability agent
    machine (readonly + memory + reflection tools, same AgentSession as the
    chat) bounded by a tool-call cap, seeded with a deterministic briefing.

    `components` carries the whole component bag (conversations live off
    `components.conn`, reports off `components.reports`, journal/
    observations/broker/data/strategies/queue/memory for the tools and
    briefing) rather than listing each dependency individually -- Task 5
    constructs exactly one Reflector, in exactly one place, so there's no
    second call site that would benefit from a narrower constructor, and the
    bag is the same one every other daily job (Consolidator, the digest)
    already takes.
    """

    llm: LLMClient
    # Duck-typed rather than `app.Components` -- see class docstring. Only
    # `.reports .conn .journal .observations .broker .data .strategies
    # .queue .memory` are ever read.
    components: Any
    settings: Settings

    def run_daily(self, now: datetime | None = None) -> str:
        et_date = _et_date(now)
        reports = self.components.reports
        # Idempotency FIRST, before touching the LLM, the conversation
        # store, or anything else -- a process restarted twice in one ET
        # trading day (or a re-invoked headless `run`) must never spend a
        # second LLM call, let alone produce a second reports row that
        # would violate the `date` UNIQUE constraint.
        if reports.exists(et_date):
            return f"already ran ({et_date})"
        return self._run(et_date)

    def _run(self, et_date: str) -> str:
        c = self.components
        conversations = ConversationStore(c.conn)
        conversation_id = conversations.start(kind="reflection")

        registry = ToolRegistry()
        register_readonly_tools(registry, data=c.data, broker=c.broker,
                                journal=c.journal, strategies=c.strategies,
                                queue=c.queue)
        register_memory_tools(registry, memory=c.memory)
        register_reflection_tools(registry, strategies=c.strategies, queue=c.queue)

        identity = load_identity()
        base_prompt = build_system_prompt(
            identity=identity, broker=c.broker, journal=c.journal,
            strategies=c.strategies, queue=c.queue, memory=c.memory)
        system_prompt = f"{base_prompt}\n{REFLECTION_INSTRUCTIONS}"

        compactor = Compactor(self.llm, conversations,
                              budget_tokens=self.settings.context_budget_tokens)
        session = AgentSession(
            self.llm, registry, system_prompt, store=conversations,
            conversation_id=conversation_id,
            max_iters=self.settings.reflection_max_iters, compactor=compactor)

        briefing = self._build_briefing(et_date)
        text = session.run_turn(briefing)

        if text.startswith("(llm error:"):
            # The LLM is down -- a corrective retry would just be a second
            # doomed call. Fail immediately, one call spent.
            return self._fail(et_date, conversation_id,
                              f"reflection failed: {text}")

        parsed = _parse_report(text)
        if parsed is None:
            # Covers both an ordinary malformed final message and the
            # LIMIT_NOTICE cap-hit text (spec §②: cap-hit still gets the one
            # corrective turn -- the transcript already holds the analysis,
            # the corrective turn just asks for it to be reproduced in the
            # required shape).
            text = session.run_turn(CORRECTIVE_PROMPT)
            parsed = _parse_report(text)
        if parsed is None:
            return self._fail(et_date, conversation_id,
                              "reflection failed: unparseable report")

        body, summary = parsed
        report_id = c.reports.add(
            date=et_date, body=body, summary=summary,
            conversation_id=conversation_id, model=getattr(self.llm, "model", ""),
            # LLMClient exposes no token-usage accounting today (see
            # llm/base.py) -- recording 0 rather than fabricating a number.
            # TODO(Task 7): wire real usage once a client surfaces it.
            tokens_used=0, status="ok")
        return f"ok: report #{report_id} ({et_date})"

    def _fail(self, et_date: str, conversation_id: int, reason: str) -> str:
        self.components.reports.add(
            date=et_date, body=reason, summary="",
            conversation_id=conversation_id, model=getattr(self.llm, "model", ""),
            tokens_used=0, status="failed")
        return reason

    def _build_briefing(self, et_date: str) -> str:
        return build_briefing(
            et_date=et_date,
            trades=self._trades_today(et_date),
            observations=self._observations_today(et_date),
            positions=self._positions_with_change(),
            pending_counts=self._pending_counts())

    def _trades_today(self, et_date: str) -> list[dict]:
        try:
            # journal.recent orders newest-first; a fetch window well above
            # MAX_TRADES so a busy day's true "today" rows aren't lost to an
            # older row from a previous day sitting in between (there
            # shouldn't be one, but the window costs nothing to keep wide).
            rows = self.components.journal.recent(limit=500)
        except Exception:  # noqa: BLE001 — a briefing must never raise
            return []
        todays = [dict(r) for r in rows if _ts_to_et_date(r["ts"]) == et_date]
        return todays[:MAX_TRADES]

    def _observations_today(self, et_date: str) -> list[dict]:
        try:
            start_utc, end_utc = _et_day_bounds_utc(et_date)
            rows = self.components.observations.recent(since_iso=start_utc, limit=5000)
        except Exception:  # noqa: BLE001 — a briefing must never raise
            return []
        todays = [dict(r) for r in rows if r["ts"] <= end_utc]
        # observations.recent (with since_iso) returns oldest-first; keep
        # the most recent MAX_OBSERVATION_LINES rather than the earliest,
        # preserving their chronological order.
        return todays[-MAX_OBSERVATION_LINES:]

    def _positions_with_change(self) -> list[dict]:
        try:
            positions = self.components.broker.get_positions()
        except Exception as exc:  # noqa: BLE001 — a briefing must never raise
            return [{"ticker": "n/a", "qty": "n/a", "avg_entry_price": "n/a",
                    "day_change": "n/a", "note": f"positions unavailable: {exc}"}]
        result = []
        for p in positions:
            day_change = "n/a"
            try:
                q = self.components.data.get_quote(p.ticker)
                if q.previous_close:
                    pct = (q.price - q.previous_close) / q.previous_close * 100
                    day_change = f"{pct:+.2f}%"
            except Exception:  # noqa: BLE001 — one bad quote must not fail the briefing
                day_change = "n/a"
            result.append({
                "ticker": p.ticker, "qty": str(p.qty),
                "avg_entry_price": str(p.avg_entry_price), "day_change": day_change})
        return result

    def _pending_counts(self) -> dict[str, int]:
        try:
            rows = self.components.queue.list()
        except Exception:  # noqa: BLE001 — a briefing must never raise
            return {}
        counts: dict[str, int] = {}
        for row in rows:
            kind = row["kind"] or "order"
            counts[kind] = counts.get(kind, 0) + 1
        return counts
