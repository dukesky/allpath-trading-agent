from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

from allpath_trade.broker.base import Broker, OrderIntent
from allpath_trade.data.base import DataSource
from allpath_trade.notify import events
from allpath_trade.notify.base import Notifier
from allpath_trade.notify.dispatch import notify_review_queued
from allpath_trade.risk.gate import RiskGate
from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account
from allpath_trade.store.app_state import AppState
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue


class QueueingOrderSink:
    """Turns an agent's order proposal into a pending review.

    The web chat cannot block on a y/N prompt, and it must never reach the
    executor itself. Queueing keeps one approval path — the same one the
    sentinel uses — so a proposal survives a closed tab or a restart, and the
    risk gate stays the single chokepoint before the broker."""

    def __init__(self, queue: ReviewQueue, gate: RiskGate, broker: Broker,
                 data: DataSource, journal: TradeJournal,
                 conversation_id: int | None = None,
                 notifier: Notifier | None = None,
                 app_state: AppState | None = None,
                 telegram_bot_token: str = "", web_base_url: str = "",
                 account: str = DEFAULT_ACCOUNT) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self.queue = queue
        self.gate = gate
        self.broker = broker
        self.data = data
        self.journal = journal
        self.conversation_id = conversation_id
        # shadow-dual-active T7: which account this chat's order proposals
        # belong to -- threaded into events.review_queued for the
        # `[Paper]`/`[Shadow]` subject prefix and, for shadow, the
        # "if approved, you'll place it yourself" clause.
        self.account = account
        # Notification wiring (all optional, default off/None): a chat order
        # proposal now goes through the same shared notify_review_queued
        # choke point (notify/dispatch.py) as sentinel.py's soft-rule
        # triggers and action_tools.py's chat strategy drafts, so it reaches
        # email/ntfy (when a notifier is configured) and the paired
        # Telegram chat with Approve/Reject buttons (when paired) --
        # neither existed for this call site before. Every caller that
        # doesn't pass these (every existing test) gets the exact same
        # "no notification at all" behavior as before this feature existed.
        self.notifier = notifier
        self.app_state = app_state
        self.telegram_bot_token = telegram_bot_token
        self.web_base_url = web_base_url

    def propose(self, intent: OrderIntent) -> str:
        preview = self._preview(intent)
        review_id = self.queue.add(
            strategy_id="", rule_id="", ticker=intent.ticker,
            rule_type="chat", condition="proposed in conversation",
            action=intent.reason,
            snapshot={"proposed_ts": datetime.now(UTC).isoformat()},
            intent=intent, source="chat",
            conversation_id=self.conversation_id, risk_preview=preview)
        self._notify_queued(review_id, intent)
        return (f"queued for the user's approval (#{review_id}). "
                f"Risk pre-check: {preview}")

    def _notify_queued(self, review_id: int, intent: OrderIntent) -> None:
        # Best-effort, never breaks the proposal: the row above is already
        # committed by the time this runs, same reasoning as
        # action_tools.py's `_notify_chat_draft_queued`. Not gated on any
        # per-strategy `notify_email` field -- there is no strategy doc here
        # (this is a bare chat order proposal), so it always attempts the
        # email/ntfy leg when a notifier is configured, matching
        # action_tools.py's own "never silenceable" chat-draft notification.
        try:
            approve_url = events.approve_link(self.web_base_url, review_id)
            subject, body = events.review_queued(
                account=self.account, review_id=review_id, ticker=intent.ticker,
                action=intent.reason, strategy_id="", approve_url=approve_url)
            notify_review_queued(
                queue=self.queue, notifier=self.notifier, app_state=self.app_state,
                telegram_bot_token=self.telegram_bot_token, review_id=review_id,
                subject=subject, body=body, account=self.account)
        except Exception as exc:  # noqa: BLE001 — see docstring above
            print(f"[order_sink] chat order notification failed: {exc}",
                  file=sys.stderr)

    def _preview(self, intent: OrderIntent) -> str:
        """Dry-run the risk gate so the card can say whether this would pass.

        Advisory only — the real gate runs again inside Executor at approval
        time, against fresh account state. A failure here (bad quote, broker
        timeout) must never stop the proposal from being queued: a user who
        can't see a pre-check is inconvenienced, a proposal that silently
        never reaches the queue is a lost instruction."""
        try:
            # Mirrors Executor.execute: a notional-sized intent needs no quote
            # at all (order_value comes straight from intent.notional), so
            # skip the network call for the common chat case and keep it
            # immune to quote-fetch flakiness.
            price = (self.data.get_quote(intent.ticker).price
                     if intent.qty is not None else Decimal(0))
            decision = self.gate.check(
                intent, account=self.broker.get_account(),
                positions=self.broker.get_positions(),
                trades_today=self.journal.trades_today(),
                is_paper=self.broker.is_paper, price=price)
        except Exception as exc:  # noqa: BLE001 — a failed preview must not block the proposal
            # Log so a persistent programming error (wrong attribute, bad
            # signature) is diagnosable from process output rather than by
            # reading rows out of SQLite — the fail-open path is silent
            # otherwise, which is how this bug survived once before.
            print(f"[order_sink] risk preview failed for {intent.ticker}: {exc}",
                  file=sys.stderr)
            return f"could not be checked ({exc})"
        if decision.approved:
            return "passes"
        return "would be rejected: " + "; ".join(decision.reasons)
