from __future__ import annotations

from datetime import UTC, datetime

from allpath_trade.broker.base import Broker, OrderIntent
from allpath_trade.data.base import DataSource
from allpath_trade.risk.gate import RiskGate
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
                 conversation_id: int | None = None) -> None:
        self.queue = queue
        self.gate = gate
        self.broker = broker
        self.data = data
        self.journal = journal
        self.conversation_id = conversation_id

    def propose(self, intent: OrderIntent) -> str:
        preview = self._preview(intent)
        review_id = self.queue.add(
            strategy_id="", rule_id="", ticker=intent.ticker,
            rule_type="chat", condition="proposed in conversation",
            action=intent.reason,
            snapshot={"proposed_ts": datetime.now(UTC).isoformat()},
            intent=intent, source="chat",
            conversation_id=self.conversation_id, risk_preview=preview)
        return (f"queued for the user's approval (#{review_id}). "
                f"Risk pre-check: {preview}")

    def _preview(self, intent: OrderIntent) -> str:
        """Dry-run the risk gate so the card can say whether this would pass.

        Advisory only — the real gate runs again inside Executor at approval
        time, against fresh account state. A failure here (bad quote, broker
        timeout) must never stop the proposal from being queued: a user who
        can't see a pre-check is inconvenienced, a proposal that silently
        never reaches the queue is a lost instruction."""
        try:
            price = self.data.get_price(intent.ticker)
            decision = self.gate.check(
                intent, account=self.broker.get_account(),
                positions=self.broker.get_positions(),
                trades_today=self.journal.trades_today(),
                is_paper=self.broker.is_paper, price=price)
        except Exception as exc:  # noqa: BLE001 — a failed preview must not block the proposal
            return f"could not be checked ({exc})"
        if decision.approved:
            return "passes"
        return "would be rejected: " + "; ".join(decision.reasons)
