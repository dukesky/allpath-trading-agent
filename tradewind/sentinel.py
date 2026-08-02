from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from tradewind.broker.base import Broker, Position
from tradewind.data.base import DataSource
from tradewind.execution import ExecutionError, Executor
from tradewind.notify.base import Notifier
from tradewind.store.reviews import ReviewError, ReviewQueue
from tradewind.strategy.actions import parse_action, to_order_intent
from tradewind.strategy.conditions import evaluate_condition
from tradewind.strategy.model import (
    Authorization,
    RuleState,
    RuleType,
    StrategyDoc,
)
from tradewind.strategy.store import StrategyStore


class TriggerOutcome(BaseModel):
    strategy_id: str
    rule_id: str
    disposition: str  # executed | rejected | queued | notified | skipped | error
    detail: str = ""


class SentinelReport(BaseModel):
    strategies_checked: int = 0
    outcomes: list[TriggerOutcome] = []
    errors: list[str] = []


class Sentinel:
    """One monitoring pass: evaluate every armed rule of every active
    strategy and dispatch triggers per (rule type x authorization)."""

    def __init__(self, strategies: StrategyStore, data: DataSource,
                 broker: Broker, executor: Executor, queue: ReviewQueue,
                 notifier: Notifier, review_agent=None) -> None:
        self.strategies = strategies
        self.data = data
        self.broker = broker
        self.executor = executor
        self.queue = queue
        self.notifier = notifier
        self.review_agent = review_agent

    def run_once(self) -> SentinelReport:
        report = SentinelReport()
        try:
            account = self.broker.get_account()
            positions = {p.ticker: p for p in self.broker.get_positions()}
        except Exception as exc:  # noqa: BLE001 — a broken env must surface, not crash
            report.errors.append(f"setup failed: {exc}")
            return report

        # A single invalid strategy YAML must not stop monitoring for every
        # other strategy — collect its error and keep going.
        docs = self.strategies.load_all(errors=report.errors)

        for doc in docs:
            try:
                self._check_strategy(doc, account.equity, positions, report)
                report.strategies_checked += 1
            except Exception as exc:  # noqa: BLE001 — isolate per-strategy failures
                report.errors.append(f"{doc.id}: {exc}")
        return report

    def _check_strategy(self, doc: StrategyDoc, equity: Decimal,
                        positions: dict[str, Position],
                        report: SentinelReport) -> None:
        ticker = doc.position.ticker
        quote = self.data.get_quote(ticker)
        position = positions.get(ticker)
        ctx = self._build_ctx(doc, quote.price, position, equity)

        for rule in doc.rules:
            if rule.state != RuleState.ARMED:
                continue
            if not evaluate_condition(rule.condition, ctx):
                continue
            # One-shot: persist TRIGGERED before any execution attempt.
            self.strategies.set_rule_state(doc.id, rule.id, RuleState.TRIGGERED)
            outcome = self._dispatch(doc, rule.id, rule.type, rule.condition,
                                     rule.action, quote.price, position, equity,
                                     ctx)
            report.outcomes.append(outcome)
            self.notifier.send(
                f"[tradewind] {doc.id}/{rule.id} triggered",
                f"strategy: {doc.name}\nrule: {rule.id} ({rule.type.value})\n"
                f"condition: {rule.condition}\naction: {rule.action}\n"
                f"price: {quote.price}\ndisposition: {outcome.disposition}"
                + (f"\ndetail: {outcome.detail}" if outcome.detail else ""))

    @staticmethod
    def _build_ctx(doc: StrategyDoc, price: Decimal, position: Position | None,
                   equity: Decimal) -> dict[str, Decimal]:
        qty = position.qty if position else Decimal(0)
        avg = position.avg_entry_price if position else Decimal(0)
        weight = (qty * price / equity) if equity > 0 else Decimal(0)
        pnl_pct = ((price - avg) / avg * 100) if avg > 0 else Decimal(0)
        plan = doc.position
        target_weight = (plan.target_weight if plan.target_weight is not None
                         else (plan.target_value / equity if equity > 0
                               else Decimal(0)))
        return {"price": price, "position_qty": qty, "position_weight": weight,
                "avg_entry_price": avg, "pnl_pct": pnl_pct,
                "target_weight": target_weight}

    def _dispatch(self, doc: StrategyDoc, rule_id: str, rule_type: RuleType,
                  condition: str, action: str, price: Decimal,
                  position: Position | None, equity: Decimal,
                  ctx: dict[str, Decimal]) -> TriggerOutcome:
        reason = f"strategy {doc.id} rule {rule_id}: {condition} -> {action}"
        intent = to_order_intent(parse_action(action), strategy=doc,
                                 rule_id=rule_id, price=price,
                                 position=position, equity=equity, reason=reason)
        snapshot = {k: str(v) for k, v in ctx.items()}

        if intent is None:
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="no actionable order (e.g. no position)")
        if doc.authorization == Authorization.NOTIFY:
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="notified")
        if (doc.authorization == Authorization.AUTO
                and rule_type == RuleType.HARD):
            try:
                result = self.executor.execute(intent)
            except Exception as exc:  # noqa: BLE001 — any executor failure must
                # still be reported and notified, not crash the sentinel pass.
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="error", detail=str(exc))
            if result.submitted:
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="executed", detail="submitted")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="rejected",
                                  detail="; ".join(result.decision.reasons))
        # confirm auth (both types), or auto+soft
        rid = self.queue.add(strategy_id=doc.id, rule_id=rule_id,
                             ticker=doc.position.ticker, rule_type=rule_type.value,
                             condition=condition, action=action,
                             snapshot=snapshot, intent=intent)
        if self.review_agent is None:
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="queued")
        return self._agent_review(rid, doc, rule_id, rule_type)

    def _agent_review(self, rid: int, doc: StrategyDoc, rule_id: str,
                      rule_type: RuleType) -> TriggerOutcome:
        base = {"strategy_id": doc.id, "rule_id": rule_id}
        # Analysis phase: the review row is still pending here, so any
        # failure genuinely leaves it "queued" — the invariant holds.
        try:
            analysis = self.review_agent.analyze(dict(self.queue.get(rid)))
            self.queue.attach_analysis(rid, analysis.model_dump_json())
        except Exception as exc:  # noqa: BLE001 — a failed review must never lose the trigger
            return TriggerOutcome(**base, disposition="queued",
                                  detail=f"agent review failed: {exc}")

        autonomous = (doc.authorization == Authorization.AUTO
                      and rule_type == RuleType.SOFT)
        if not autonomous:
            return TriggerOutcome(**base, disposition="queued",
                                  detail="analysis attached: "
                                         f"{analysis.recommendation} — "
                                         f"{analysis.reasoning[:300]}")

        # Decision phase: once approve()/reject() claims the row, its status
        # is authoritative in the DB — dispositions here must match it, not
        # be papered over by a broad except.
        if analysis.recommendation == "execute":
            try:
                result = self.queue.approve(rid)
            except ExecutionError as exc:
                return TriggerOutcome(
                    **base, disposition="error",
                    detail=f"agent-approved but execution failed: {exc}")
            except ReviewError as exc:
                return TriggerOutcome(
                    **base, disposition="error",
                    detail=f"review already resolved elsewhere: {exc}")
            detail = ("agent-approved; submitted" if result.submitted else
                      "agent-approved; risk gate rejected: "
                      + "; ".join(result.decision.reasons))
            return TriggerOutcome(**base, disposition="executed", detail=detail)

        try:
            self.queue.reject(rid, note=analysis.reasoning[:500])
        except ReviewError as exc:
            return TriggerOutcome(
                **base, disposition="error",
                detail=f"review already resolved elsewhere: {exc}")
        return TriggerOutcome(**base, disposition="skipped",
                              detail=f"agent skip: {analysis.reasoning[:300]}")
