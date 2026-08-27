from __future__ import annotations

import sys
import time as time_module
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
from allpath_trade.llm.base import LLMClient, LLMResponse
from allpath_trade.notify import events
from allpath_trade.notify.base import Notifier, send_report
from allpath_trade.scheduler import ET, ts_to_et_date
from allpath_trade.store.accounts import DEFAULT_ACCOUNT
from allpath_trade.store.reviews import ReviewError, RevisionValidationError

# Seed-briefing hard caps (spec §②: "种子简报...全部 fence_external 围栏").
# Each is independent -- a chatty observation day can't starve the trades
# block, and vice versa. The per-block char cap is the last-resort backstop
# against any one block blowing past a sane prompt size; for the trades
# block it's the cap that actually *binds* in practice, not MAX_TRADES=30 --
# a 30-trade day with a rendered `reason` (up to 80 chars each, see
# _format_trade) plus the rest of a formatted line comfortably clears 2000
# chars. MAX_TRADE_CHARS is sized (3000) for the realistic 30-trade case;
# trades are already newest-first (TradeJournal.recent is `ORDER BY id
# DESC`), so tail-truncation still drops the oldest trades first, same as
# the plain char cap. The observations block gets its own truncation
# function (_cap_observation_chars) instead, because it's chronologically
# ordered oldest->newest and tail-truncation there would cut the newest
# (most relevant) rows.
MAX_TRADES = 30
MAX_TRADE_CHARS = 3000
MAX_TRADE_REASON_CHARS = 80
MAX_OBSERVATION_LINES = 50
MAX_BLOCK_CHARS = 2000
MAX_STRATEGY_CHARS = 3000
MAX_THESIS_CHARS = 300
MAX_SUMMARY_CHARS = 600

# Finding F4: one shared deadline for the whole day-change quote loop below
# (Reflector._positions_with_change), not a per-ticker timeout -- same
# precedent as web/routes/dashboard.py's QUOTES_BUDGET_SECONDS. Without it, a
# single hung `data.get_quote` call at 16:05 (Yahoo down, DNS wedged, ...)
# stalls not just this one position's day-change but the entire daily chain
# behind it: run_daily blocks, the sentinel's next tick can't start behind
# the same broker/data pool, and the reflection report itself is never
# written. Positions whose quote isn't fetched before the deadline lands
# render "n/a" (the same fallback an individual quote failure already used)
# rather than block indefinitely.
QUOTES_BUDGET_SECONDS = 10


def _monotonic() -> float:
    """Indirection over `time.monotonic` for I8's reflection deadline, so
    tests can drive it by hand (the dashboard's `_utcnow` pattern) instead
    of sleeping through a real 30-minute budget."""
    return time_module.monotonic()


# The text a deadline-expired iteration returns in place of a provider call
# (see _DeadlineGuard). Deliberately NOT a REPORT/SUMMARY-shaped string: it
# has to fail `_parse_report` so `_run` falls into exactly the same
# corrective-wrap-up branch AgentSession's own LIMIT_NOTICE already
# triggers, rather than needing a second, parallel wrap-up path.
DEADLINE_NOTICE = "(stopped: reflection time budget reached — wrapping up)"


class _DeadlineGuard:
    """Wraps this pass's LLMClient to give the reflection session a
    wall-clock bound (I8, `Settings.reflection_deadline_seconds`) alongside
    its tool-call bound.

    Why a wrapper and not a check inside AgentSession's loop: the loop is
    the SHARED agent machine (agent/loop.py, also driving live chat), and
    the deadline is a reflection-specific policy -- a chat session has a
    human watching it and no nightly chain queued behind it. `complete` is
    called exactly once per loop iteration, at the top, so checking here IS
    the "between iterations" check, and it also catches time burned by a
    slow TOOL round-trip, not just by the provider.

    Once expired, `complete` returns `DEADLINE_NOTICE` WITHOUT calling the
    provider: the iteration cap's own exit path (unparseable text -> one
    corrective wrap-up turn) is reused verbatim, so a timed-out pass still
    produces a real report. `_run` clears `enforcing` before that wrap-up
    turn -- the whole point of the deadline is to reach the wrap-up, so
    gating the wrap-up on the same expired deadline would turn every
    timeout into a `failed` row.

    Everything other than `complete` (`model`, and anything a future client
    exposes) forwards to the wrapped client via `__getattr__`."""

    def __init__(self, inner: LLMClient, deadline_seconds: int) -> None:
        self._inner = inner
        self._deadline = (_monotonic() + deadline_seconds
                          if deadline_seconds > 0 else None)
        self.enforcing = True
        self.expired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def complete(self, messages: list[dict], tools: Any = None):
        if (self.enforcing and self._deadline is not None
                and _monotonic() >= self._deadline):
            self.expired = True
            return LLMResponse(text=DEADLINE_NOTICE)
        return self._inner.complete(messages, tools=tools)


_TRUNCATION_NOTE = "\n... (truncated)"
_FRONT_TRUNCATION_NOTE = "... (older observations truncated)\n"

# Reproduces the literal REPORT/SUMMARY template from REFLECTION_INSTRUCTIONS
# and names the one failure mode that actually recurs: a bare `SUMMARY` line
# is required -- not "## SUMMARY", not "SUMMARY:". This is the one nudge a
# reflection session gets before the run is recorded as failed.
CORRECTIVE_PROMPT = (
    "Your last message did not end with the exact REPORT/SUMMARY structure "
    "this pass requires. Reproduce it now, exactly, nothing else:\n\n"
    "REPORT\n"
    "<Day summary>\n"
    "<Per-strategy check>\n"
    "<Lessons>\n"
    "<Proposals>\n"
    "SUMMARY\n"
    "<3-5 plain sentences suitable for a phone push notification>\n\n"
    "The line containing only the word SUMMARY -- a bare line, not "
    "\"## SUMMARY\" or \"SUMMARY:\" -- marks where the report ends and the "
    "notification text begins. Both sections are required and must be "
    "non-empty."
)

REFLECTION_INSTRUCTIONS = """\
## Daily reflection

You are now running the end-of-day reflection pass, not a live chat with the
user. Nobody is watching this in real time -- the Reports page replays the
transcript afterward, and only a short push-notification summary goes out
immediately. You have no order tool and no way to apply anything yourself:
every conclusion you act on goes through memory_update (curated memory) or
propose_strategy_revision (queued for the user's approval on the Pending
page) -- you are advisory only.

A deterministic seed briefing appears in the first user message below: every
active strategy's thesis and rules, today's trades, today's observations,
today's position day-changes, and the pending queue, each fenced as
external content. Start from those known facts; spend your tool calls
investigating what looks off, not re-deriving what the briefing already
told you.

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


def _et_day_bounds_utc(et_date: str) -> tuple[str, str]:
    """UTC ISO bounds of the ET calendar day `et_date`, used only as a cheap
    SQL-level pre-filter (`ObservationLog.window`'s `since_iso`/`until_iso`)
    -- the exact per-row cut still happens with `ts_to_et_date`."""
    d = date.fromisoformat(et_date)
    start_et = datetime.combine(d, time.min, tzinfo=ET)
    end_et = datetime.combine(d, time.max, tzinfo=ET)
    return start_et.astimezone(UTC).isoformat(), end_et.astimezone(UTC).isoformat()


def _parse_report(text: str) -> tuple[str, str] | None:
    """Split on the LAST line that is exactly "SUMMARY" (spec §②). Both the
    report body and the summary must be non-empty, or the transcript doesn't
    actually carry the required structure and this is a parse failure.

    The stored body also has the leading `REPORT` marker line -- and any
    preamble before it (e.g. a stray "Sure, here you go.") -- stripped, so
    `reports.body` starts at the actual report content. This keys off the
    LAST bare `REPORT` line, symmetric with the SUMMARY split. When no
    `REPORT` line is present at all, parsing stays lenient: a message that
    goes straight into report content with a valid SUMMARY split still
    stores -- the parse contract only strictly requires the SUMMARY line."""
    lines = text.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if line.strip() == "SUMMARY":
            idx = i
    if idx is None:
        return None
    report_lines = lines[:idx]
    report_idx = None
    for i, line in enumerate(report_lines):
        if line.strip() == "REPORT":
            report_idx = i
    if report_idx is not None:
        report_lines = report_lines[report_idx + 1:]
    body = "\n".join(report_lines).strip()
    summary = "\n".join(lines[idx + 1:]).strip()
    if not body or not summary:
        return None
    return body, summary


def _cap_chars(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    """Tail-truncate: keeps the FRONT of `text`, drops the end. Correct for
    every block except observations -- trades are newest-first, strategies
    aren't recency-ordered at all, so preserving the front is the right
    default in both cases."""
    if len(text) <= limit:
        return text
    return text[: max(limit - len(_TRUNCATION_NOTE), 0)] + _TRUNCATION_NOTE


def _cap_observation_chars(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    """Front-truncate whole lines: keeps the END of `text`, drops the start.
    Observations are chronologically ordered oldest->newest (see
    Reflector._observations_today); on overflow the newest rows -- the
    afternoon and close, exactly what the reflection exists to review --
    must survive, not the stale morning tail that `_cap_chars` would keep."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    budget = limit - len(_FRONT_TRUNCATION_NOTE)
    while lines and len("\n".join(lines)) > budget:
        lines.pop(0)
    return _FRONT_TRUNCATION_NOTE + "\n".join(lines)


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
        # Fill-honesty round: append the actual execution time when known,
        # after the existing "filled qty @ price" text (not interleaved
        # with it) so callers still matching that exact substring keep
        # working. filled_at can be absent on rows journaled before this
        # column existed, or if a refresh hasn't landed yet -- degrade to
        # the plain fill line rather than claim a time we don't have.
        filled_at = str(t.get("filled_at") or "")[:19]
        if filled_at:
            fill += f" at {filled_at}"
    line = (f"{str(t.get('ts', ''))[:19]} {t.get('side')} {t.get('ticker')} "
            f"size={size} status={t.get('status')} {fill} "
            f"strategy={t.get('strategy_id') or '-'}")
    reason = str(t.get("reason") or "")[:MAX_TRADE_REASON_CHARS]
    if reason:
        line += f" reason={reason}"
    return line


def _format_observation(o: dict) -> str:
    subject = o.get("subject") or "-"
    return f"{str(o.get('ts', ''))[:19]} [{o.get('source')}/{subject}] {o.get('text')}"


def _format_position(p: dict) -> str:
    note = f" ({p['note']})" if p.get("note") else ""
    return (f"{p.get('ticker')} qty={p.get('qty')} "
            f"avg_entry={p.get('avg_entry_price', 'n/a')} "
            f"day_change={p.get('day_change', 'n/a')}{note}")


def _format_strategy(s: dict) -> str:
    """One strategy's thesis and rules -- the fields build_system_prompt's
    own per-strategy line omits (id/status/rule-states only, see
    agent/context.py:45-49). Without this block the model can only learn a
    strategy's actual thesis/conditions/actions/target via a read_strategy
    tool call, spending part of the 12-call session budget per strategy."""
    thesis = (s.get("thesis") or "").strip()
    if len(thesis) > MAX_THESIS_CHARS:
        thesis = thesis[:MAX_THESIS_CHARS] + "..."
    lines = [(f"{s.get('id')} [{s.get('status')}] {s.get('name')} "
              f"target={s.get('target', 'n/a')}")]
    if thesis:
        lines.append(f"  thesis: {thesis}")
    rules = s.get("rules") or []
    if rules:
        for r in rules:
            lines.append(f"  {r.get('condition')} -> {r.get('action')} "
                         f"[{r.get('state')}]")
    else:
        lines.append("  no rules")
    return "\n".join(lines)


def _format_target(position: Any) -> str:
    """Renders a `strategy.model.PositionPlan`'s target as a display string.
    `_has_target`'s model validator (strategy/model.py) guarantees exactly
    one of `target_weight`/`target_value` is set."""
    if position.target_weight is not None:
        return f"{position.target_weight * 100:.1f}%"
    if position.target_value is not None:
        return f"${position.target_value}"
    return "n/a"


def build_briefing(*, et_date: str, trades: list[dict], observations: list[dict],
                   positions: list[dict], pending_counts: dict[str, int],
                   strategies: list[dict] | None = None,
                   account: str = DEFAULT_ACCOUNT) -> str:
    """Pure, deterministic seed briefing -- no LLM, no I/O. `trades` /
    `observations` / `positions` / `strategies` are already-fetched plain
    dicts (the Reflector does the DB/broker/data-source/YAML reads); this
    function only formats and caps them. Each block is
    `fence_external`-wrapped independently (spec §②: "全部 fence_external
    围栏") since every value inside ultimately traces back to model-authored
    strings (trade `reason`, observation `text`, strategy `thesis`) or a
    remote price feed -- data, not instructions.

    `account` defaults to `DEFAULT_ACCOUNT` ("paper") so every existing
    direct-format test that predates the shadow account keeps passing
    unchanged; the Reflector's own `_build_briefing` always passes its
    bundle's real account explicitly. Rendered plainly in the header, not
    fenced -- unlike the blocks below it never traces back to model- or
    feed-authored content, it's a fact this function itself is asserting."""
    strategies_block = "\n".join(_format_strategy(s) for s in (strategies or []))
    strategies_block = strategies_block or "no active strategies"

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
        f"Account: {account}",
        "\n## Strategies (thesis & rules)",
        fence_external(_cap_chars(strategies_block, MAX_STRATEGY_CHARS)),
        "\n## Today's trades",
        fence_external(_cap_chars(trades_block, MAX_TRADE_CHARS)),
        "\n## Today's observations",
        fence_external(_cap_observation_chars(obs_block)),
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
    # shadow-dual-active T4: an `app.AccountComponents` bundle (one
    # account's own broker/journal/queue/strategies/memory/observations/
    # search/conn) -- duck-typed rather than importing that type here (see
    # class docstring) to avoid a reflect.py <-> app.py import cycle. Only
    # `.reports .conn .conversations .journal .observations .search .broker
    # .data .strategies .queue .memory` are ever read.
    components: Any
    settings: Settings
    # Optional, defaulting to None, so Task 4's tests (which construct a
    # Reflector with no notifier at all) keep passing unchanged. None means
    # "don't push" -- app.py only ever wires a real one in once an LLM is
    # actually configured, but a dead notification channel must never be
    # able to break the reflection run itself (see _run's use of it below).
    notifier: Notifier | None = None

    def run_daily(self, now: datetime | None = None) -> str:
        et_date = _et_date(now)
        reports = self.components.reports
        # Idempotency FIRST, before touching the LLM, the conversation
        # store, or anything else -- a process restarted twice in one ET
        # trading day (or a re-invoked headless `run`) must never spend a
        # second LLM call, let alone produce a second reports row that
        # would violate the `date` UNIQUE constraint.
        # I9: `exists_ok`, not `exists` -- a `status="failed"` row (see
        # `_fail`: LLM down, or output unparseable twice) records an
        # ATTEMPT, not a result. Guarding on bare existence let one bad
        # 16:05 minute lock the account out of reflection for the whole
        # calendar day, with nothing to retry into; `ReportStore.add`'s
        # UPSERT is what lets the retry replace that row.
        if reports.exists_ok(et_date):
            return f"already ran ({et_date})"
        return self._run(et_date)

    def _run(self, et_date: str) -> str:
        c = self.components
        # shadow-dual-active T4: `c.conversations` is already this
        # account's own ConversationStore (built once in app.py's per-
        # account bundle), not a fresh unscoped instance constructed here
        # -- constructing a bare `ConversationStore(c.conn)` would default
        # to the paper account regardless of which account this Reflector
        # actually belongs to.
        conversations = c.conversations
        conversation_id = conversations.start(kind="reflection")

        registry = ToolRegistry()
        register_readonly_tools(registry, data=c.data, broker=c.broker,
                                journal=c.journal, strategies=c.strategies,
                                queue=c.queue)
        # search=c.search (this account's own SessionSearch, built in
        # app.py) so session_search -- advertised right in
        # REFLECTION_INSTRUCTIONS's tool list -- actually exists in the
        # registry (mirrors web/chat_service.py's wiring) AND stays scoped
        # to this account: a bare `SessionSearch(c.conn)` would default to
        # paper and leak the other account's turns/observations into this
        # session's search results. Without registering it at all, the
        # first call the model makes to it burns an iteration on
        # "error: unknown tool" out of the 12-call session budget.
        register_memory_tools(registry, memory=c.memory, search=c.search)
        # Experiment mode (spec 2026-08-26, Settings.experiment_auto_apply_
        # revisions): collect the plain-int review ids THIS run's own
        # session proposes, so the success path below can auto-approve
        # exactly those rows -- never a stale row queued by an earlier run
        # or by a chat proposal. `on_proposed` stays None (the default) when
        # the flag is off, so a non-experiment run allocates nothing extra
        # and behaves exactly as before this change.
        proposed_rids: list[int] = []
        auto_apply = self.settings.experiment_auto_apply_revisions
        register_reflection_tools(
            registry, strategies=c.strategies, queue=c.queue,
            on_proposed=proposed_rids.append if auto_apply else None)

        identity = load_identity()
        base_prompt = build_system_prompt(
            identity=identity, broker=c.broker, journal=c.journal,
            strategies=c.strategies, queue=c.queue, memory=c.memory,
            # Important 1: the Reflector always knows its own account (this
            # bundle) -- pass it so the system prompt carries the
            # paper/shadow section (agent/context.py's ACCOUNT_NOTES),
            # never omitted here the way an as-yet-unwired chat caller
            # might leave it.
            account=c.account)
        system_prompt = f"{base_prompt}\n{REFLECTION_INSTRUCTIONS}"

        compactor = Compactor(self.llm, conversations,
                              budget_tokens=self.settings.context_budget_tokens)
        # I8: the session talks to the provider THROUGH the deadline guard;
        # the compactor deliberately does not (a compaction is bookkeeping
        # the pass needs to keep its own history coherent, and handing it a
        # DEADLINE_NOTICE in place of a summary would corrupt the stored
        # conversation, not shorten the night).
        guard = _DeadlineGuard(self.llm, self.settings.reflection_deadline_seconds)
        session = AgentSession(
            guard, registry, system_prompt, store=conversations,
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
            #
            # max_iters is per-run_turn on AgentSession, but the tool-call
            # budget the spec describes is per-SESSION -- without capping it
            # here, a cap-hit on the first turn would hand the corrective
            # retry a second fresh `reflection_max_iters`, silently doubling
            # the session's real budget. The corrective turn's only job is
            # to extract/reformat what the transcript already holds, not to
            # do more research, so one iteration is always enough.
            session.max_iters = 1
            # I8: the wrap-up turn is exempt from the wall-clock deadline
            # -- reaching it is the entire point of the deadline, and a
            # pass that timed out has an even better reason than a
            # cap-hit one to be asked for the report the transcript
            # already holds. One iteration, so the exemption can't extend
            # the night by more than a single provider call.
            guard.enforcing = False
            text = session.run_turn(CORRECTIVE_PROMPT)
            if text.startswith("(llm error:"):
                return self._fail(
                    et_date, conversation_id,
                    f"reflection failed: llm error on corrective turn: {text}")
            parsed = _parse_report(text)
        if parsed is None:
            return self._fail(et_date, conversation_id,
                              "reflection failed: unparseable report")

        body, summary = parsed
        # ntfy payload insurance: reports.add's summary becomes the push
        # notification body (Task 5/7 wiring) -- cap it defensively here so
        # a verbose model response can never blow past a sane push size.
        summary = summary[:MAX_SUMMARY_CHARS]
        report_id = c.reports.add(
            date=et_date, body=body, summary=summary,
            conversation_id=conversation_id, model=getattr(self.llm, "model", ""),
            # LLMClient exposes no token-usage accounting today (see
            # llm/base.py) -- recording 0 rather than fabricating a number.
            # TODO(Task 7): wire real usage once a client surfaces it.
            tokens_used=0, status="ok")
        # Experiment mode: only on the success path, and only AFTER the
        # report row is durably stored above -- a `_fail` run returns before
        # reaching this line, so it never auto-applies anything, and a crash
        # between the report write and this call just leaves the row
        # pending for a human, same as today.
        if auto_apply and proposed_rids:
            self._auto_apply_revisions(proposed_rids)
        if self.notifier is not None:
            # Only a stored "ok" report notifies -- a "failed" one (LLM
            # down, or unparseable output twice, see _fail) does NOT push:
            # the Reports page already shows it as failed, there is nothing
            # actionable for the user to act on at 4am, and paging them for
            # a broken-LLM night would just be noise they'd learn to ignore.
            subject, full_body = events.daily_report(
                account=c.account, date=et_date, summary=summary, body=body)
            # Push failure must never fail the *run* -- the report is
            # already durably stored above; a dead notification channel is
            # a notify-layer problem, not a reflection-layer one. The bool
            # is intentionally ignored beyond this one stderr line, same
            # convention as every other notifier caller in this codebase
            # (e.g. notify/email.py, notify/ntfy.py).
            if not send_report(self.notifier, subject, summary, full_body):
                print(f"[reflection] notification failed ({et_date})",
                     file=sys.stderr)
        return f"ok: report #{report_id} ({et_date})"

    def _fail(self, et_date: str, conversation_id: int, reason: str) -> str:
        self.components.reports.add(
            date=et_date, body=reason, summary="",
            conversation_id=conversation_id, model=getattr(self.llm, "model", ""),
            tokens_used=0, status="failed")
        return reason

    def _auto_apply_revisions(self, rids: list[int]) -> None:
        """Experiment mode (spec 2026-08-26): approve the revision rows THIS
        run just queued, through the exact applier chain a human approval
        uses -- every guard (freeze, byte-exact base, version monotonicity)
        still decides. A guard rejection leaves the row pending, same as
        today, and the paper trail lands in observations either way."""
        c = self.components
        for rid in rids:
            try:
                row = dict(c.queue.get(rid))
            except ReviewError:
                continue  # superseded/vanished between propose and now
            if row["status"] != "pending" or row["kind"] != "strategy_revision":
                continue
            strategy_id = row["strategy_id"]
            try:
                c.queue.approve(rid)
            except (RevisionValidationError, ReviewError) as exc:
                c.observations.add(
                    "reflection_auto_apply",
                    f"revision #{rid} for {strategy_id} NOT auto-applied "
                    f"({exc}); left pending for human review",
                    subject=row["ticker"])
                continue
            warning = c.strategies.rearm_warning(strategy_id)
            c.observations.add(
                "reflection_auto_apply",
                f"revision #{rid} for {strategy_id} auto-applied "
                f"(experiment mode).{warning}",
                subject=row["ticker"])

    def _build_briefing(self, et_date: str) -> str:
        return build_briefing(
            et_date=et_date,
            strategies=self._strategies_today(),
            trades=self._trades_today(et_date),
            observations=self._observations_today(et_date),
            positions=self._positions_with_change(),
            pending_counts=self._pending_counts(),
            account=self.components.account)

    def _strategies_today(self) -> list[dict]:
        try:
            errors: list[str] = []
            docs = self.components.strategies.load_all(status=None, errors=errors)
        except Exception:  # noqa: BLE001 — a briefing must never raise
            return []
        result = []
        for d in docs:
            result.append({
                "id": d.id, "name": d.name, "status": d.status.value,
                "target": _format_target(d.position), "thesis": d.thesis,
                "rules": [{"condition": r.condition, "action": r.action,
                          "state": r.state.value} for r in d.rules]})
        return result

    def _trades_today(self, et_date: str) -> list[dict]:
        try:
            # journal.recent orders newest-first; a fetch window well above
            # MAX_TRADES so a busy day's true "today" rows aren't lost to an
            # older row from a previous day sitting in between (there
            # shouldn't be one, but the window costs nothing to keep wide).
            rows = self.components.journal.recent(limit=500)
        except Exception:  # noqa: BLE001 — a briefing must never raise
            return []
        todays = [dict(r) for r in rows if ts_to_et_date(r["ts"]) == et_date]
        return todays[:MAX_TRADES]

    def _observations_today(self, et_date: str) -> list[dict]:
        try:
            start_utc, end_utc = _et_day_bounds_utc(et_date)
            # `window` fetches newest-first and pushes both bounds into SQL
            # (Task 4 review finding #2) -- `observations.recent`'s
            # oldest-first `ORDER BY id ASC LIMIT` would silently drop the
            # newest rows on a day with more than `limit` observations,
            # losing exactly the afternoon/close window the reflection
            # exists to review.
            rows = self.components.observations.window(start_utc, end_utc, limit=5000)
        except Exception:  # noqa: BLE001 — a briefing must never raise
            return []
        todays = [dict(r) for r in rows if ts_to_et_date(r["ts"]) == et_date]
        # `window` returns newest-first; reverse to chronological order for
        # display, then keep the newest MAX_OBSERVATION_LINES.
        todays.reverse()
        return todays[-MAX_OBSERVATION_LINES:]

    def _positions_with_change(self) -> list[dict]:
        try:
            positions = self.components.broker.get_positions()
        except Exception as exc:  # noqa: BLE001 — a briefing must never raise
            return [{"ticker": "n/a", "qty": "n/a", "avg_entry_price": "n/a",
                    "day_change": "n/a", "note": f"positions unavailable: {exc}"}]
        result = []
        # F4: one shared deadline for this whole loop -- see
        # QUOTES_BUDGET_SECONDS' module comment. Checked before each
        # get_quote call, not just once up front: a position already past
        # the deadline never starts a new call at all, it renders "n/a"
        # immediately, same as an individual quote failure.
        deadline = time_module.monotonic() + QUOTES_BUDGET_SECONDS
        for p in positions:
            day_change = "n/a"
            if time_module.monotonic() < deadline:
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
