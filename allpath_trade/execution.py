from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from allpath_trade.broker.base import Broker, Order, OrderIntent
from allpath_trade.data.base import DataSource
from allpath_trade.risk.gate import RiskDecision, RiskGate
from allpath_trade.store.journal import TradeJournal


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
        # Notional intents don't need a price for the gate's checks (order_value
        # comes straight from intent.notional), so only fetch a quote when qty
        # sizing requires converting shares to dollars.
        try:
            price = (self.data.get_quote(intent.ticker).price
                     if intent.qty is not None else Decimal(0))
            account = self.broker.get_account()
            positions = self.broker.get_positions()
            trades_today = self.journal.trades_today()
        except Exception as exc:
            failed = RiskDecision(approved=False,
                                  reasons=[f"data error: {exc}"])
            self.journal.record(intent, failed, None, status_override="error")
            raise ExecutionError(str(exc)) from exc

        decision = self.gate.check(
            intent,
            account=account,
            positions=positions,
            trades_today=trades_today,
            is_paper=self.broker.is_paper,
            price=price,
        )
        if not decision.approved:
            self.journal.record(intent, decision, None)
            return ExecutionResult(submitted=False, order=None, decision=decision)

        try:
            order = self.broker.submit_order(intent)
        except Exception as exc:
            failed = RiskDecision(approved=False,
                                  reasons=[f"broker error: {exc}"])
            self.journal.record(intent, failed, None, status_override="error")
            raise ExecutionError(str(exc)) from exc

        self.journal.record(intent, decision, order)
        return ExecutionResult(submitted=True, order=order, decision=decision)
