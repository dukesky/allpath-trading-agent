from __future__ import annotations

import contextlib
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from allpath_trade.broker.base import (
    Broker,
    BrokerNotConfigured,
    Order,
    OrderIntent,
    Position,
)
from allpath_trade.data.base import DataSource
from allpath_trade.execution import ExecutionError, Executor
from allpath_trade.notify import events
from allpath_trade.notify.base import Notifier
from allpath_trade.notify.dispatch import notify_review_queued, push_telegram_receipt
from allpath_trade.store.accounts import DEFAULT_ACCOUNT
from allpath_trade.store.app_state import AppState
from allpath_trade.store.reviews import ReviewError, ReviewQueue
from allpath_trade.strategy.actions import parse_action, to_order_intent
from allpath_trade.strategy.conditions import evaluate_condition
from allpath_trade.strategy.model import (
    Authorization,
    RuleState,
    RuleType,
    StrategyDoc,
)
from allpath_trade.strategy.store import StrategyStore


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
                 notifier: Notifier, review_agent=None, observations=None,
                 web_base_url: str = "", app_state: AppState | None = None,
                 telegram_bot_token: str = "", account: str = DEFAULT_ACCOUNT) -> None:
        # shadow-dual-active T7: which account THIS Sentinel instance
        # belongs to (app.py constructs one per account) -- threaded into
        # every notify.events builder call below for the `[Paper]`/
        # `[Shadow]` subject prefix and the shadow-specific order wording.
        # Named distinctly from `run_once`'s local `account` (the broker's
        # `Account` balance object, an unrelated pre-existing local) --
        # `self.account` is always the plain account STRING.
        self.account = account
        # C3: the one word this module uses for "the executor accepted it".
        # For shadow that executor is `ShadowExecutor` writing a ledger row
        # (broker/shadow.py has no brokerage behind it), so "submitted" --
        # which travels into the notification body, the `TriggerOutcome`
        # detail, and through that the run report and CLI output -- would
        # claim an order exists somewhere that it does not.
        self._placed = "recorded" if account == "shadow" else "submitted"
        self.strategies = strategies
        self.data = data
        self.broker = broker
        self.executor = executor
        self.queue = queue
        self.notifier = notifier
        self.review_agent = review_agent
        self.observations = observations
        # Approve-by-link (Part A): empty (the default) means every
        # `_notify_queued` call below builds no link at all, same behavior
        # as before this feature existed. See config.py's Settings.web_base_url.
        self.web_base_url = web_base_url
        # Telegram push (both optional, default off): a queued review also
        # reaches the paired chat with Approve/Reject buttons, and an
        # auto-executed hard rule's order_result receipt also reaches it
        # (no buttons) -- see notify/dispatch.py. `app_state` is None and
        # `telegram_bot_token` is "" in every test that doesn't care about
        # Telegram, which is exactly the "Telegram is off" state
        # `notify.dispatch`'s push functions already no-op on.
        self.app_state = app_state
        self.telegram_bot_token = telegram_bot_token

    def run_once(self) -> SentinelReport:
        report = SentinelReport()
        try:
            account = self.broker.get_account()
            positions = {p.ticker: p for p in self.broker.get_positions()}
        except BrokerNotConfigured:
            # setup-wizard T1: not an outage -- the user simply hasn't
            # finished setup. Caught BEFORE the generic handler below so the
            # report carries one flat, actionable line instead of "setup
            # failed: Alpaca keys are not set — finish setup", whose
            # "setup failed" prefix reads like something broke. No strategy
            # is evaluated either way: every rule needs live equity and
            # positions, so there is nothing meaningful to check.
            report.errors.append("paper broker not configured")
            return report
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
                if self.observations is not None:
                    # Distinct source (not "sentinel") so a quote-fetch or
                    # other per-strategy failure can never be mistaken for a
                    # real rule trigger by anything counting "sentinel" rows
                    # (the daily digest's trigger count) — even if a third
                    # writer is added later, string-matching the text would
                    # drift silently but a dedicated source cannot.
                    self.observations.add("sentinel_error", f"{doc.id}: {exc}")
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
            if self.observations is not None:
                self.observations.add(
                    "sentinel",
                    f"{doc.id}/{rule.id} {rule.condition} -> {rule.action}: "
                    f"{outcome.disposition} {outcome.detail}".strip(),
                    subject=doc.position.ticker)

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
        ticker = doc.position.ticker

        if intent is None:
            # Nothing was proposed (e.g. a sell rule fired with no position
            # held) — there is no order to report, but the rule itself did
            # fire and the user has no other way to learn that, so still
            # notify (briefly) rather than staying silent.
            self._notify_rule(doc, rule_id, condition, "skipped")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="no actionable order (e.g. no position)")
        if doc.authorization == Authorization.NOTIFY:
            self._notify_rule(doc, rule_id, condition, "notified")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="notified")
        if (doc.authorization == Authorization.AUTO
                and rule_type == RuleType.HARD):
            try:
                result = self.executor.execute(intent)
            except Exception as exc:  # noqa: BLE001 — any executor failure must
                # still be reported and notified, not crash the sentinel pass.
                self._notify_order(doc, ticker, intent.side.value, False, str(exc))
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="error", detail=str(exc))
            if result.submitted:
                self._notify_order(doc, ticker, intent.side.value, True, self._placed,
                                   order=result.order)
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="executed", detail=self._placed)
            detail = "; ".join(result.decision.reasons)
            self._notify_order(doc, ticker, intent.side.value, False, detail)
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="rejected", detail=detail)
        # confirm auth (both types), or auto+soft
        rid = self.queue.add(strategy_id=doc.id, rule_id=rule_id,
                             ticker=ticker, rule_type=rule_type.value,
                             condition=condition, action=action,
                             snapshot=snapshot, intent=intent)
        if self.review_agent is None:
            self._notify_queued(doc, rid, ticker, action, "", price=price, intent=intent)
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="queued")
        return self._agent_review(rid, doc, rule_id, rule_type, condition,
                                  action, intent, price=price)

    def _agent_review(self, rid: int, doc: StrategyDoc, rule_id: str,
                      rule_type: RuleType, condition: str, action: str,
                      intent: OrderIntent, *, price: Decimal | None = None) -> TriggerOutcome:
        base = {"strategy_id": doc.id, "rule_id": rule_id}
        ticker = doc.position.ticker
        # Analysis phase: the review row is still pending here, so any
        # failure genuinely leaves it "queued" — the invariant holds.
        try:
            analysis = self.review_agent.analyze(dict(self.queue.get(rid)))
            self.queue.attach_analysis(rid, analysis.model_dump_json())
        except Exception as exc:  # noqa: BLE001 — a failed review must never lose the trigger
            self._notify_queued(doc, rid, ticker, action, "", price=price, intent=intent)
            return TriggerOutcome(**base, disposition="queued",
                                  detail=f"agent review failed: {exc}")

        if analysis.reasoning.startswith("unparseable analysis:"):
            # The LLM's output couldn't be parsed as a recommendation at all —
            # this is not a genuine "skip" decision, so don't act on it.
            # Leave the trigger pending for human review.
            self._notify_queued(doc, rid, ticker, action, "", price=price, intent=intent)
            return TriggerOutcome(
                **base, disposition="queued",
                detail="analysis unparseable — left for human review")

        autonomous = (doc.authorization == Authorization.AUTO
                      and rule_type == RuleType.SOFT)
        if not autonomous:
            recommendation = f"{analysis.recommendation} — {analysis.reasoning[:300]}"
            self._notify_queued(doc, rid, ticker, action, recommendation,
                                price=price, intent=intent)
            return TriggerOutcome(**base, disposition="queued",
                                  detail="analysis attached: " + recommendation)

        # Decision phase: once approve()/reject() claims the row, its status
        # is authoritative in the DB — dispositions here must match it, not
        # be papered over by a broad except.
        if analysis.recommendation == "execute":
            try:
                result = self.queue.approve(rid)
            except ExecutionError as exc:
                detail = f"agent-approved but execution failed: {exc}"
                self._notify_order(doc, ticker, intent.side.value, False, detail)
                return TriggerOutcome(**base, disposition="error", detail=detail)
            except ReviewError as exc:
                detail = f"review already resolved elsewhere: {exc}"
                self._notify_order(doc, ticker, intent.side.value, False, detail)
                return TriggerOutcome(**base, disposition="error", detail=detail)
            detail = (f"agent-approved; {self._placed}" if result.submitted else
                      "agent-approved; risk gate rejected: "
                      + "; ".join(result.decision.reasons))
            disposition = "executed" if result.submitted else "rejected"
            self._notify_order(doc, ticker, intent.side.value, result.submitted, detail,
                               order=result.order)
            return TriggerOutcome(**base, disposition=disposition, detail=detail)

        try:
            self.queue.reject(rid, note=analysis.reasoning[:500])
        except ReviewError as exc:
            detail = f"review already resolved elsewhere: {exc}"
            self._notify_order(doc, ticker, intent.side.value, False, detail)
            return TriggerOutcome(**base, disposition="error", detail=detail)
        # The agent actively decided not to act, and the review is already
        # resolved, so there's no order to report — but the rule the user
        # wrote still fired, and this is the only place they'd learn that.
        self._notify_rule(doc, rule_id, condition, "skipped")
        return TriggerOutcome(**base, disposition="skipped",
                              detail=f"agent skip: {analysis.reasoning[:300]}")

    def _notify_rule(self, doc: StrategyDoc, rule_id: str, condition: str,
                     disposition: str) -> None:
        subject, body = events.rule_triggered(
            account=self.account, strategy_id=doc.id, rule_id=rule_id,
            ticker=doc.position.ticker, condition=condition, disposition=disposition)
        self._send(doc, subject, body)

    def _notify_order(self, doc: StrategyDoc, ticker: str, side: str, submitted: bool,
                      detail: str, *, order: Order | None = None) -> None:
        # `order` (the executor's own `Order`, when there is one -- see the
        # AUTO+HARD and agent-approved AUTO+SOFT call sites above) supplies
        # `filled_qty`/`filled_avg_price` for the shadow "place this order
        # in your brokerage now: BUY 4.5 TSLA @ ~$332.01" wording
        # (events.order_result's own docstring) -- absent on every path
        # that never reached the executor (an exception, a risk-gate
        # rejection), which is fine: that shadow wording only fires when
        # `submitted=True` anyway.
        subject, body = events.order_result(
            account=self.account, ticker=ticker, side=side, submitted=submitted,
            detail=detail, filled_qty=order.filled_qty if order else None,
            filled_avg_price=order.filled_avg_price if order else None)
        self._send(doc, subject, body)
        # "自动执行了也要通知我" -- the order_result receipt (auto-executed hard
        # rule, and every other order outcome that flows through this same
        # method) also reaches the paired Telegram chat, no buttons since
        # there's nothing left to approve/reject. Independent of
        # doc.notify_email (see notify_review_queued's docstring for why the
        # Telegram leg is never gated by that email-only preference) and of
        # `self.web_base_url` (no link involved here at all). One message.
        push_telegram_receipt(app_state=self.app_state,
                              telegram_bot_token=self.telegram_bot_token, body=body,
                              account=self.account)

    def _notify_queued(self, doc: StrategyDoc, review_id: int, ticker: str, action: str,
                       recommendation: str, *, price: Decimal | None = None,
                       intent: OrderIntent | None = None) -> None:
        # Part B: the price context available at the instant this item was
        # queued -- the exact sample the rule triggered on, not a second,
        # separately re-fetched "live" quote (see review_queued's
        # docstring for why). `est_shares` only makes sense for a
        # notional-sized intent -- a qty-sized one already says its share
        # count plainly in `action`.
        trigger_price = f"${price:,.2f}" if price is not None else ""
        est_shares = ""
        if intent is not None and intent.qty is None and intent.notional and price:
            with contextlib.suppress(ArithmeticError, InvalidOperation):
                est_shares = f"{intent.notional / price:.2f}"
        # Part A: only when the operator opted in (Settings -> Access) AND
        # this review actually has a live token -- see `events.approve_link`
        # (shared with agent/action_tools.py's chat strategy drafts) for the
        # `getattr`-based degrade-to-no-link rationale.
        approve_url = events.approve_link(self.web_base_url, review_id)
        subject, body = events.review_queued(
            account=self.account, review_id=review_id, ticker=ticker, action=action,
            strategy_id=doc.id, recommendation=recommendation,
            trigger_price=trigger_price, est_shares=est_shares,
            approve_url=approve_url)
        # notify_email gates the email/ntfy leg only (see _send's own
        # docstring) -- notify_review_queued (notify/dispatch.py) is the
        # shared choke point this method now shares with order_sink.py's
        # chat order proposals and action_tools.py's chat strategy drafts;
        # its Telegram leg (buttons) is unconditional whenever a chat is
        # paired, same reasoning as _notify_order's receipt push above.
        notify_review_queued(
            queue=self.queue, notifier=self.notifier, app_state=self.app_state,
            telegram_bot_token=self.telegram_bot_token, review_id=review_id,
            subject=subject, body=body, account=self.account,
            notify_email=doc.notify_email)

    def _send(self, doc: StrategyDoc, subject: str, body: str) -> None:
        # notify_email is a notification preference, not a trading
        # parameter -- it gates delivery of the email/push here only. By the
        # time any _notify_* method runs, the disposition (executed, queued,
        # rejected, recorded) has already happened; skipping the send never
        # skips that.
        if not doc.notify_email:
            return
        self.notifier.send(subject, body)
