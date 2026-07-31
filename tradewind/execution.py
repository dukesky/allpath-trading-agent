from __future__ import annotations

from pydantic import BaseModel

from tradewind.broker.base import Broker, Order, OrderIntent
from tradewind.data.base import DataSource
from tradewind.risk.gate import RiskDecision, RiskGate
from tradewind.store.journal import TradeJournal


class ExecutionError(Exception):
    pass


class ExecutionResult(BaseModel):
    submitted: bool
    order: Order | None
    decision: RiskDecision


class Executor:
    """The single entry point for trading. Everything above (scheduler,
    agent tools) creates OrderIntents and calls execute()."""

    def __init__(self, broker: Broker, gate: RiskGate,
                 journal: TradeJournal, data: DataSource) -> None:
        self.broker = broker
        self.gate = gate
        self.journal = journal
        self.data = data

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        quote = self.data.get_quote(intent.ticker)
        decision = self.gate.check(
            intent,
            account=self.broker.get_account(),
            positions=self.broker.get_positions(),
            trades_today=self.journal.trades_today(),
            is_paper=self.broker.is_paper,
            price=quote.price,
        )
        if not decision.approved:
            self.journal.record(intent, decision, None)
            return ExecutionResult(submitted=False, order=None, decision=decision)

        try:
            order = self.broker.submit_order(intent)
        except Exception as exc:
            failed = RiskDecision(approved=False,
                                  reasons=[f"broker error: {exc}"])
            self.journal.record(intent, failed, None)
            raise ExecutionError(str(exc)) from exc

        self.journal.record(intent, decision, order)
        return ExecutionResult(submitted=True, order=order, decision=decision)
