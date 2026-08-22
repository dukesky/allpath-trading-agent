from __future__ import annotations

import difflib
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.execution import ExecutionError, Executor
from allpath_trade.notify import events
from allpath_trade.notify.base import Notifier
from allpath_trade.notify.dispatch import notify_review_queued
from allpath_trade.store.accounts import DEFAULT_ACCOUNT
from allpath_trade.store.app_state import AppState
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.loader import (
    StrategyValidationError,
    atomic_write_text,
    is_valid_strategy_id,
    parse_strategy_text,
)
from allpath_trade.strategy.store import StrategyStore

# Import-time only: the agent package must not depend on the web package at
# runtime (wrong dependency direction — the terminal CLI's tool layer would
# drag in the web subpackage). A Protocol would decouple this fully, but the
# annotation is the only use, and the test's duck-typed Sink shows a
# structural type isn't needed here yet — TYPE_CHECKING is the smaller fix.
if TYPE_CHECKING:
    from allpath_trade.web.order_sink import QueueingOrderSink


def register_action_tools(registry: ToolRegistry, *, strategies: StrategyStore,
                          executor: Executor,
                          confirm: Callable[[str], bool],
                          order_sink: QueueingOrderSink | None = None,
                          queue: ReviewQueue | None = None,
                          conversation_id: int | None = None,
                          notifier: Notifier | None = None,
                          web_base_url: str = "",
                          app_state: AppState | None = None,
                          telegram_bot_token: str = "",
                          account: str = DEFAULT_ACCOUNT) -> None:

    def _notify_chat_draft_queued(doc, review_id, *, is_new: bool) -> None:
        # Spec §④/降级: a notification failure must never affect queueing --
        # the row above is already committed by the time this runs, so the
        # whole thing is best-effort and swallows everything.
        #
        # Important 1: deliberately NOT gated on `doc.notify_email` -- that
        # field is a per-strategy preference written by the PROPOSER (the
        # LLM) into the very draft this call is about. A chat draft is
        # user-initiated -- the user is mid-conversation right now -- so
        # this notification is the loud confirmation channel that a draft
        # is sitting there awaiting approval; the whole point is that it
        # must never be silenceable by the proposal itself. Gating on
        # `notify_email` let an injected `authorization: auto` +
        # `notify_email: false` draft queue with ZERO human-visible signal.
        # Reflection's own notify path (sentinel.py) is untouched -- this
        # only covers chat-sourced drafts.
        # `queue is not None` here whenever this callback can even fire (it's
        # only invoked from the web-mode branch below, which requires
        # `queue` in the first place) -- notify_review_queued still needs it
        # explicitly since it reads the row back for the Telegram nonce.
        if notifier is None and not telegram_bot_token:
            return
        try:
            action = (f"create new strategy '{doc.id}'" if is_new
                      else f"revise strategy '{doc.id}' to v{doc.version}")
            approve_url = events.approve_link(web_base_url, review_id)
            subject, body = events.review_queued(
                account=account, review_id=review_id, ticker=doc.position.ticker,
                action=action, strategy_id=doc.id, approve_url=approve_url,
                kind="strategy_revision")
            notify_review_queued(
                queue=queue, notifier=notifier, app_state=app_state,
                telegram_bot_token=telegram_bot_token, review_id=review_id,
                subject=subject, body=body, account=account)
        except Exception as exc:  # noqa: BLE001 — see docstring above
            print(f"[action_tools] chat draft notification failed: {exc}",
                  file=sys.stderr)

    def draft_strategy(strategy_id: str, yaml_text: str, reason: str) -> str:
        if not is_valid_strategy_id(strategy_id):
            return f"error: invalid strategy id {strategy_id!r}"
        try:
            doc = parse_strategy_text(strategy_id, yaml_text)
        except StrategyValidationError as exc:
            return f"error: {'; '.join(exc.errors)}"
        path = strategies.directory / f"{strategy_id}.yaml"
        is_new = not path.exists()
        old_text = "" if is_new else path.read_text()
        if old_text:
            try:
                current = parse_strategy_text(strategy_id, old_text)
                if doc.version <= current.version:
                    doc = doc.model_copy(update={"version": current.version + 1})
            except StrategyValidationError:
                pass  # unreadable current file: keep drafted version
        # Minor 1: floor at draft time, same silent-bump design as the
        # current-version bump above. Without this, a new-strategy draft
        # (is_new -- no `old_text` to bump against) or a repair draft
        # against an unreadable current file (the `except` above, which
        # deliberately keeps the drafted version rather than guessing) can
        # carry `version: 0` (or an omitted/negative one) straight into
        # `new_text` below. The applier's own gate (strategy/apply.py) then
        # rejects every approval attempt with "version must be a positive
        # integer" -- the row queues, can never be approved, and bounces
        # back to pending forever with no way for the user to fix it short
        # of re-drafting from scratch.
        if doc.version < 1:
            doc = doc.model_copy(update={"version": 1})
        # The version written into `new_text` below is the FINAL one (after
        # the bump/floor above) in both modes, so a queued diff shows what
        # will actually land if approved, not what was originally typed.
        new_text = yaml.safe_dump(doc.model_dump(mode="json"), sort_keys=False,
                                  allow_unicode=True)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"{strategy_id}.yaml (current)",
            tofile=f"{strategy_id}.yaml (proposed)"))
        if queue is not None:
            # Web/Telegram mode: queue the draft as a strategy_revision
            # proposal (spec 2026-08-12-chat-strategy-proposals-design.md)
            # instead of blocking on confirm() (web mode's confirm is always
            # `lambda _: False`). `queue` being explicitly injected -- rather
            # than reusing propose_order's `order_sink is not None` -- is
            # the mode discriminator here: draft_strategy and propose_order
            # are independent tools with independent web-mode wiring, and an
            # explicit kwarg says so at the call site instead of relying on
            # a sibling tool's parameter as an implicit signal.
            try:
                # Important 2: ADD before SUPERSEDE, not the reverse. The
                # old ordering committed the supersede first, so an
                # `add_strategy_revision` failure right after it left the
                # prior draft marked superseded ("replaced by a newer chat
                # draft") while Pending held nothing new -- a false audit
                # note and a lost draft. Adding first means a failure here
                # (the except below) never reaches supersede at all, so the
                # prior draft stays exactly as it was. `exclude_id=rid`
                # keeps the row just inserted (same strategy_id, same
                # source='chat', already status='pending') from matching
                # supersede's own WHERE clause and superseding itself; the
                # note now names the real replacing id instead of a static
                # string (closes the spec §③ resolution_note-names-new-id
                # gap).
                rid = queue.add_strategy_revision(
                    strategy_id=strategy_id, ticker=doc.position.ticker,
                    old_yaml=old_text, new_yaml=new_text, diff=diff,
                    rationale=reason, conversation_id=conversation_id,
                    source="chat", is_new=is_new)
                superseded = queue.supersede_pending_chat_revision(
                    strategy_id, note=f"replaced by #{rid}", exclude_id=rid)
            except Exception as exc:  # noqa: BLE001 — spec 降级: a queue
                # failure must return an error string, not lose the drafted
                # YAML (it's still right there in the conversation).
                return f"error: could not queue draft: {exc}"
            _notify_chat_draft_queued(doc, rid, is_new=is_new)
            msg = (f"Draft queued for your approval as #{rid} — open Pending "
                   "(or tap the link in the notification). It will not "
                   "take effect until you approve it.")
            if superseded is not None:
                msg += f" (replaced draft #{superseded})"
            return msg
        if not confirm(f"Save strategy '{strategy_id}' v{doc.version}?"
                       f" Reason: {reason}\n{diff or new_text}"):
            return "user declined"
        atomic_write_text(path, new_text)
        strategies.snapshot_version(doc, reason)
        return f"saved {strategy_id} v{doc.version}"

    def propose_order(ticker: str, side: str, reason: str,
                      qty: str | None = None, notional: str | None = None) -> str:
        try:
            intent = OrderIntent(
                ticker=ticker, side=OrderSide(side.lower()),
                qty=Decimal(str(qty)) if qty is not None else None,
                notional=Decimal(str(notional)) if notional is not None else None,
                reason=reason)
        except (ValidationError, ValueError, InvalidOperation) as exc:
            return f"error: invalid order: {exc}"
        if order_sink is not None:
            # Web chat: no blocking confirm() here — queue it and return, the
            # user resolves it with a button. The terminal path below is
            # untouched and still used when order_sink is not injected.
            return order_sink.propose(intent)
        size = f"qty {intent.qty}" if intent.qty else f"${intent.notional}"
        if not confirm(f"Submit order: {intent.side.value} {size} "
                       f"{intent.ticker}? Reason: {reason}"):
            return "user declined"
        try:
            result = executor.execute(intent)
        except ExecutionError as exc:
            return f"execution error: {exc}"
        if result.submitted:
            oid = result.order.id if result.order else ""
            if account == "shadow":
                return (f"recorded order {oid} in the shadow ledger — the user must "
                        "place it in their brokerage themselves").strip()
            return f"submitted order {oid}".strip()
        return "rejected by risk gate: " + "; ".join(result.decision.reasons)

    t = "string"
    registry.register(
        "draft_strategy",
        "Draft or revise a strategy YAML -- including a brand-new strategy, "
        "not just changes to an existing one. In web/Telegram chat this "
        "queues the draft as a proposal for the user's approval on the "
        "Pending page and does not save anything by itself; in a terminal "
        "session the user confirms inline before it's saved. A version "
        "snapshot is recorded once the draft is approved (or saved, in a "
        "terminal). Optional fields `horizon` (long/medium/swing) and "
        "`bias` (bullish/bearish/neutral) drive the strategies page's "
        "at-a-glance chips -- include them when the thesis makes the time "
        "horizon and market direction clear, but leave them out rather "
        "than guessing when it doesn't.",
        {"type": "object", "properties": {
            "strategy_id": {"type": t}, "yaml_text": {"type": t},
            "reason": {"type": t}},
         "required": ["strategy_id", "yaml_text", "reason"]},
        draft_strategy)
    registry.register(
        "propose_order",
        "Propose a market order (buy/sell). The user must confirm; the order "
        "then passes the deterministic risk gate.",
        {"type": "object", "properties": {
            "ticker": {"type": t}, "side": {"type": t, "enum": ["buy", "sell"]},
            "qty": {"type": t}, "notional": {"type": t}, "reason": {"type": t}},
         "required": ["ticker", "side", "reason"]},
        propose_order)
