from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from allpath_trade import market_hours
from allpath_trade.broker.base import (
    Broker,
    BrokerNotConfigured,
    OptionInstruction,
    OptionIntent,
    OptionPositionRef,
    Order,
    OrderIntent,
    OrderSide,
    Position,
    parse_occ_symbol,
)
from allpath_trade.broker.options_mcp import OptionPick, OptionsBackend, OptionsBackendError
from allpath_trade.data.base import DataSource
from allpath_trade.execution import (
    ExecutionError,
    Executor,
    close_underlying_options,
    option_positions_for,
)
from allpath_trade.market_hours import ET
from allpath_trade.notify import events
from allpath_trade.notify.base import Notifier
from allpath_trade.notify.dispatch import notify_review_queued, push_telegram_receipt
from allpath_trade.risk.breaker import DrawdownBreaker
from allpath_trade.store.accounts import DEFAULT_ACCOUNT
from allpath_trade.store.app_state import AppState
from allpath_trade.store.reviews import ReviewError, ReviewQueue
from allpath_trade.strategy.actions import (
    ActionError,
    ActionKind,
    ActionSpec,
    is_option_action,
    parse_action,
    to_order_intent,
)
from allpath_trade.strategy.conditions import evaluate_condition
from allpath_trade.strategy.model import (
    Authorization,
    Rule,
    RuleState,
    RuleType,
    StrategyDoc,
)
from allpath_trade.strategy.store import StrategyStore

# Incident 2026-08-28: a manual `allpath-trade check` run (cli.py's
# cmd_check calls Sentinel.run_once() directly, with none of scheduler.py's
# own `is_market_hours()` gate around it) dispatched five option entry
# orders at 1:32am ET. Alpaca's options venue doesn't accept orders outside
# regular hours, and the MCP server's rejection payload had no `id` field --
# Executor.execute_option now treats that as a placement failure (see its
# own comment), but by the time that fix runs the one-shot rule has already
# burned into a phantom "submitted" row. This helper is the other half of
# the fix: keep an option rule ARMED while the market is closed instead of
# letting it dispatch into a guaranteed failure at all.
#
# US regular session (Mon-Fri 09:30-16:00 ET): no holiday calendar yet
# (see docs/TODO.md) -- a holiday or early close still reads as "open" here.
# That's judged acceptable for now: on a holiday, Executor.execute_option's
# id-less-payload guard turns the resulting rejection into a loud, journaled
# error (and, for a buy rule, still burns the one-shot the way any other
# rejected order would) instead of the silent phantom-submitted row this
# incident was about -- not a silent failure any more, just not pre-empted.


def _us_market_open_now() -> bool:
    """US regular session, weekday Mon-Fri 09:30-16:00 America/New_York."""
    return market_hours.is_us_market_open()


def _now() -> datetime:
    """Wall clock for re-arm cooldowns; a function so tests can move time."""
    return datetime.now(UTC)


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
                 telegram_bot_token: str = "", account: str = DEFAULT_ACCOUNT,
                 breaker: DrawdownBreaker | None = None,
                 options_backend: OptionsBackend | None = None) -> None:
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
        # Task 7: the drawdown circuit breaker (risk/breaker.py) is
        # optional -- None (the default) means run_once behaves exactly as
        # it did before this breaker existed. app.py builds one per
        # account whenever app_state is available.
        self.breaker = breaker
        # Task 6: the options MCP backend -- optional (None by default), so
        # every Sentinel constructed without it (every pre-existing call
        # site, and any account with options_trading off) behaves exactly
        # as it did before options existed: option ActionKinds mainly reach
        # `_dispatch_option` because the strategy loader's AUTHORING-time
        # check requires authorization: auto or confirm plus a hard rule
        # (strategy/loader.py) -- but that check doesn't re-run on a plain
        # LOAD, so a strategy the breaker later demoted auto -> confirm
        # still loads and dispatches here. `_dispatch_option` itself stays
        # defensive against `None` here (the operator can turn the flag off
        # with strategies still on disk) rather than trusting either
        # invariant alone.
        self.options_backend = options_backend

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

        if self.breaker is not None:
            trip = self.breaker.check(account.equity)
            if trip is not None:
                subject, body = events.drawdown_halt(
                    account=self.account, peak=trip.peak, equity=trip.equity,
                    drawdown=trip.drawdown, demoted=trip.demoted)
                # Account-level halt, not a per-strategy notification
                # preference -- send directly rather than through `_send`,
                # so it reaches the operator even when every strategy on
                # file has `notify_email: false`.
                if self.notifier is not None:
                    self.notifier.send(subject, body)
                push_telegram_receipt(
                    app_state=self.app_state,
                    telegram_bot_token=self.telegram_bot_token, body=body,
                    account=self.account)
                if self.observations is not None:
                    # Distinct source ("breaker", not "sentinel") for the
                    # same digest-count reason as `sentinel_error` above --
                    # the daily digest counts "sentinel" rows as rule
                    # triggers, and a breaker trip is not one.
                    self.observations.add(
                        "breaker",
                        f"drawdown breaker tripped: {trip.drawdown:.1%} below "
                        f"peak {trip.peak}; demoted: "
                        f"{', '.join(trip.demoted) or 'none'}")
                report.errors.append(
                    f"drawdown breaker tripped ({trip.drawdown:.1%}); "
                    "auto strategies demoted to confirm")

        # Task 6: expiry safety sweep -- account-level, not tied to any one
        # strategy's rules, so it runs here rather than inside
        # `_check_strategy`. Only when options are actually enabled for
        # this account (mirrors every other `options_backend is None`
        # no-op guard in this module). Any option position within one
        # calendar day of expiry is closed unconditionally: v1 never
        # exercises a position (spec §"Out of scope"), so an option left
        # open through expiry would simply expire worthless/be auto-
        # exercised by the OCC with nobody watching.
        # Incident 2026-08-28 (see _us_market_open_now's own comment): a
        # sell-to-close order can't execute while the market is closed
        # either -- without this gate, every overnight sentinel tick would
        # now generate a loud, journaled "no order id" error row (via
        # Executor.execute_option's id-less-payload guard) for every
        # expiring position, instead of just quietly waiting for the next
        # pass during market hours.
        if self.options_backend is not None and _us_market_open_now():
            self._run_expiry_sweep(positions, report)

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

    def _run_expiry_sweep(self, positions: dict[str, Position],
                          report: SentinelReport) -> None:
        today = datetime.now(UTC).date()
        # Finding 2b: a position already carrying an open sell order (e.g.
        # a close_options rule for the same strategy fired earlier in this
        # same sentinel pass, or a prior sweep's sell-to-close is still
        # unfilled/unresolved from a previous pass) must not get a second
        # sell-to-close piled on top of it. Queried once per sweep, not per
        # position -- one `get_orders` round trip either way. Best-effort:
        # a broker failure here must not skip the sweep itself (the whole
        # point of this sweep is that it's the last line of defense against
        # an expiring position), just its dedup -- proceed as if nothing
        # has an open sell.
        try:
            open_sell_symbols = {
                o.ticker for o in self.broker.get_orders(open_only=True)
                if o.side == OrderSide.SELL
            }
        except Exception:  # noqa: BLE001 — best-effort filter, see above
            open_sell_symbols = set()
        for p in positions.values():
            parts = parse_occ_symbol(p.ticker)
            if parts is None or (parts.expiry - today).days > 1:
                continue
            if p.ticker in open_sell_symbols:
                continue
            try:
                intent = OptionIntent(
                    underlying=parts.root, right=parts.right, occ_symbol=p.ticker,
                    side=OrderSide.SELL, qty=int(p.qty), est_premium=Decimal(0),
                    reason="expiry safety sweep (DTE<=1)")
                result = self.executor.execute_option(intent)
            except Exception as exc:  # noqa: BLE001 — one bad position must not
                # stop the sweep for the rest of the positions, nor crash the pass.
                report.errors.append(f"expiry sweep {p.ticker}: {exc}")
                continue
            detail = self._placed if result.submitted else "; ".join(result.decision.reasons)
            # No `StrategyDoc` exists for this account-level sweep, so unlike
            # `_notify_order` this send is never gated by a per-strategy
            # `notify_email` -- built directly here, mirroring the breaker
            # block's own ungated `events.*` + `notifier.send` +
            # `push_telegram_receipt` pattern just above.
            subject, body = events.order_result(
                account=self.account, ticker=p.ticker, side="sell",
                submitted=result.submitted, detail=detail,
                filled_qty=result.order.filled_qty if result.order else None,
                filled_avg_price=result.order.filled_avg_price if result.order else None)
            if self.notifier is not None:
                self.notifier.send(subject, body)
            push_telegram_receipt(
                app_state=self.app_state, telegram_bot_token=self.telegram_bot_token,
                body=body, account=self.account)
            if self.observations is not None:
                # Distinct source ("options_sweep", not "sentinel") for the
                # same digest-count reason as "breaker" above -- an expiry
                # close is not a rule trigger.
                self.observations.add(
                    "options_sweep",
                    f"expiry sweep closed {p.ticker}: "
                    f"{'executed' if result.submitted else 'rejected'} {detail}".strip(),
                    subject=parts.root)
            if not result.submitted:
                report.errors.append(f"expiry sweep {p.ticker}: rejected: {detail}")

    def _check_strategy(self, doc: StrategyDoc, equity: Decimal,
                        positions: dict[str, Position],
                        report: SentinelReport) -> None:
        ticker = doc.position.ticker
        quote = self.data.get_quote(ticker)
        position = positions.get(ticker)
        ctx = self._build_ctx(doc, quote.price, position, equity)

        for rule in doc.rules:
            if (rule.state == RuleState.TRIGGERED and rule.rearm is not None
                    and self._should_rearm(doc.id, rule)):
                self.strategies.set_rule_state(doc.id, rule.id, RuleState.ARMED)
                rule.state = RuleState.ARMED
                if self.observations is not None:
                    self.observations.add(
                        "sentinel_rearm",
                        f"{doc.id}/{rule.id} re-armed after {rule.rearm}m cooldown",
                        subject=doc.position.ticker)
            if rule.state != RuleState.ARMED:
                continue
            if not evaluate_condition(rule.condition, ctx):
                continue
            spec = parse_action(rule.action)
            if is_option_action(spec) and not _us_market_open_now():
                # Incident 2026-08-28: do NOT burn this rule. Alpaca's
                # options venue rejects orders while the market is closed,
                # so dispatching now would only produce a guaranteed
                # failure (see Executor.execute_option's id-less-payload
                # guard) -- and for a one-shot rule, that failure is
                # unrecoverable: the rule would already read TRIGGERED and
                # never get another chance to fire. Leaving it ARMED means
                # it fires on the first tick after the market reopens
                # instead. This applies to every option kind, buys AND
                # close_options alike -- an exit rule (close_options) must
                # survive the night still armed and ready to protect the
                # position, not burn into a failed order while the market
                # is closed. Stock rules are unaffected: their market
                # orders queue fine outside hours (see
                # execution.refresh_pending_fills's own docstring on DAY
                # orders queued overnight), so they keep today's behavior.
                continue
            # One-shot: persist TRIGGERED before any execution attempt.
            self.strategies.set_rule_state(doc.id, rule.id, RuleState.TRIGGERED)
            self.strategies.record_fire(doc.id, rule.id, _now())
            outcome = self._dispatch(doc, rule.id, rule.type, rule.condition,
                                     rule.action, spec, quote.price, position,
                                     equity, ctx, positions)
            report.outcomes.append(outcome)
            if self.observations is not None:
                self.observations.add(
                    "sentinel",
                    f"{doc.id}/{rule.id} {rule.condition} -> {rule.action}: "
                    f"{outcome.disposition} {outcome.detail}".strip(),
                    subject=doc.position.ticker)

    def _should_rearm(self, strategy_id: str, rule: Rule) -> bool:
        """Spec 2026-09-26-rule-rearm-design.md, decision 2. Option buys never
        re-arm, even from a hand-edited file that authoring would have
        rejected: each re-fire would pick and stack another contract."""
        try:
            if parse_action(rule.action).kind in (ActionKind.BUY_CALL, ActionKind.BUY_PUT):
                return False
        except ActionError:
            return False
        if not _us_market_open_now():
            return False
        now = _now()
        last = self.strategies.last_fire(strategy_id, rule.id)
        if last is not None and now - last < timedelta(minutes=rule.rearm):
            return False
        day_start = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return (self.strategies.fire_count_since(strategy_id, rule.id, day_start)
                < rule.max_fires_per_day)

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
                  condition: str, action: str, spec: ActionSpec, price: Decimal,
                  position: Position | None, equity: Decimal,
                  ctx: dict[str, Decimal],
                  positions: dict[str, Position]) -> TriggerOutcome:
        reason = f"strategy {doc.id} rule {rule_id}: {condition} -> {action}"
        # `spec` is already parsed by `_check_strategy` (it needs
        # `is_option_action(spec)` itself, for the closed-market ARM-don't-
        # burn check above) -- reusing it here avoids parsing `action`
        # twice for the same rule on the same tick.
        if is_option_action(spec):
            # Option ActionKinds must never reach `to_order_intent` below --
            # it raises on them by design (strategy/actions.py). Auth
            # branching for option actions (auto vs confirm, and the
            # breaker-tripped exception for close_options) now lives inside
            # `_dispatch_option` itself -- see docs/superpowers/specs/
            # 2026-09-14-option-pending-queue-design.md §2.
            return self._dispatch_option(doc, rule_id, condition, rule_type, spec, price,
                                         positions, reason, action=action)
        intent = to_order_intent(spec, strategy=doc,
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

    def _dispatch_option(self, doc: StrategyDoc, rule_id: str, condition: str,
                         rule_type: RuleType, spec: ActionSpec, price: Decimal,
                         positions: dict[str, Position],
                         reason: str, action: str) -> TriggerOutcome:
        if self.options_backend is None:
            # Defensive: AUTHORING-time validation (strategy/loader.py's
            # `authoring` param) requires every option action to sit on a
            # hard rule on an auto/confirm strategy, but LOADING deliberately
            # does not re-enforce that -- a strategy the drawdown breaker
            # demoted from auto to confirm must keep loading (see
            # docs/superpowers/specs/2026-09-14-option-pending-queue-design.md
            # §2) -- and loading can't guarantee the operator left
            # options_trading on either. Turning that flag off must degrade
            # this one rule to a reported error, never crash the whole pass.
            self._notify_rule(doc, rule_id, condition, "error")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="error",
                                  detail="options trading disabled")
        if spec.kind == ActionKind.CLOSE_OPTIONS:
            if rule_type != RuleType.HARD:
                self._notify_rule(doc, rule_id, condition, "skipped")
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="skipped",
                                      detail="close_options requires rule type: hard")
            if doc.authorization == Authorization.NOTIFY:
                # notify never creates an approvable row, for options any
                # more than for stock (spec 2026-09-14 §2) -- notify only
                # notifies.
                self._notify_rule(doc, rule_id, condition, "notified")
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="notified")
            # auto executes as before. A confirm strategy's close waits for the
            # user -- EXCEPT once the drawdown breaker has tripped: loading
            # tolerates a demoted (auto->confirm) strategy (see the guard
            # above), and a halt must never strand an exit behind an approval
            # (spec 2026-09-14 §2).
            if doc.authorization == Authorization.AUTO or self._breaker_tripped():
                return self._dispatch_close_options(doc, rule_id, condition, positions, reason)
            return self._queue_option_close(doc, rule_id, rule_type, condition, action,
                                            positions, price, reason)
        # BUY_CALL / BUY_PUT
        if rule_type != RuleType.HARD:
            self._notify_rule(doc, rule_id, condition, "skipped")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="option buys require rule type: hard")
        if doc.authorization == Authorization.NOTIFY:
            # notify only notifies -- see the CLOSE_OPTIONS branch above.
            self._notify_rule(doc, rule_id, condition, "notified")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="notified")
        if doc.authorization == Authorization.AUTO:
            return self._dispatch_option_buy(doc, rule_id, condition, spec, price, reason)
        return self._queue_option_buy(doc, rule_id, rule_type, condition, action,
                                      spec, price, reason)

    def _breaker_tripped(self) -> bool:
        return self.breaker is not None and self.breaker.tripped_at() is not None

    def _pick_option_contract(
            self, doc: StrategyDoc, rule_id: str, condition: str, spec: ActionSpec,
            price: Decimal
    ) -> tuple[str, int, Decimal, OptionPick | None, TriggerOutcome | None]:
        """Resolve a buy_call/buy_put rule's contract params (right/min_dte/
        otm_pct, defaults applied) and pick a contract -- shared by the
        immediate-execute (`_dispatch_option_buy`, auto) and queue-for-
        approval (`_queue_option_buy`, confirm) paths, which used to each
        carry their own copy of this defaults+pick_contract+error/None
        handling almost line for line.

        Returns `(right, min_dte, otm_pct, pick, early_outcome)`.
        `early_outcome` is non-None when the caller should return it as-is
        without building an order/instruction -- an `OptionsBackendError`
        ("error", already notified) or `pick is None` ("skipped: no
        affordable option contract", already notified). Otherwise `pick` is
        the chosen `OptionPick` and `early_outcome` is None."""
        right = "call" if spec.kind == ActionKind.BUY_CALL else "put"
        # Defaults (dte 7, otm 2%) are applied HERE, not by the parser --
        # strategy/actions.py's ActionSpec deliberately leaves them None
        # when the rule text omits them (see its own docstring).
        min_dte = spec.min_dte if spec.min_dte is not None else 7
        otm_pct = spec.otm_pct if spec.otm_pct is not None else Decimal("0.02")
        underlying = doc.position.ticker
        try:
            pick = self.options_backend.pick_contract(
                underlying, right, min_dte, otm_pct, spec.amount, price)
        except OptionsBackendError as exc:
            self._notify_rule(doc, rule_id, condition, "error")
            return right, min_dte, otm_pct, None, TriggerOutcome(
                strategy_id=doc.id, rule_id=rule_id, disposition="error", detail=str(exc))
        if pick is None:
            # Not an error -- pick_contract's own contract (broker/
            # options_mcp.py) returns None rather than raising when no
            # affordable/tradable contract exists.
            self._notify_rule(doc, rule_id, condition, "skipped")
            return right, min_dte, otm_pct, None, TriggerOutcome(
                strategy_id=doc.id, rule_id=rule_id, disposition="skipped",
                detail="no affordable option contract")
        return right, min_dte, otm_pct, pick, None

    def _queue_option_buy(self, doc: StrategyDoc, rule_id: str, rule_type: RuleType,
                          condition: str, action: str, spec: ActionSpec,
                          price: Decimal, reason: str) -> TriggerOutcome:
        underlying = doc.position.ticker
        right, min_dte, otm_pct, pick, early = self._pick_option_contract(
            doc, rule_id, condition, spec, price)
        if early is not None:
            return early
        instruction = OptionInstruction(
            op="buy", underlying=underlying, reason=reason, strategy_id=doc.id,
            right=right, min_dte=min_dte, otm_pct=otm_pct, budget=spec.amount,
            spot_at_trigger=price)
        snapshot = {"price": str(price), "preview": json.loads(pick.model_dump_json())}
        rid = self.queue.add_option_order(
            strategy_id=doc.id, rule_id=rule_id, ticker=underlying,
            rule_type=rule_type.value, condition=condition, action=action,
            snapshot=snapshot, instruction=instruction)
        preview = f"{pick.qty}x {pick.occ_symbol} ≈ ${pick.est_premium:,.2f}"
        return self._queued_option_review(rid, doc, rule_id, rule_type, condition,
                                          action, price, preview)

    def _queue_option_close(self, doc: StrategyDoc, rule_id: str, rule_type: RuleType,
                            condition: str, action: str,
                            positions: dict[str, Position], price: Decimal,
                            reason: str) -> TriggerOutcome:
        underlying = doc.position.ticker
        refs = [OptionPositionRef(occ_symbol=p.ticker, qty=int(p.qty))
               for p in option_positions_for(underlying, positions.values())
               if int(p.qty) >= 1]
        if not refs:
            self._notify_rule(doc, rule_id, condition, "skipped")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="no option positions to close")
        instruction = OptionInstruction(op="close", underlying=underlying, reason=reason,
                                        strategy_id=doc.id, positions_at_trigger=refs)
        snapshot = {"price": str(price),
                   "preview": [json.loads(r.model_dump_json()) for r in refs]}
        rid = self.queue.add_option_order(
            strategy_id=doc.id, rule_id=rule_id, ticker=underlying,
            rule_type=rule_type.value, condition=condition, action=action,
            snapshot=snapshot, instruction=instruction)
        preview = "close " + ", ".join(f"{r.occ_symbol} x{r.qty}" for r in refs)
        return self._queued_option_review(rid, doc, rule_id, rule_type, condition,
                                          action, price, preview)

    def _queued_option_review(self, rid: int, doc: StrategyDoc, rule_id: str,
                              rule_type: RuleType, condition: str, action: str,
                              price: Decimal, preview: str) -> TriggerOutcome:
        ticker = doc.position.ticker
        if self.review_agent is None:
            self._notify_queued(doc, rid, ticker, action, "", price=price,
                                kind="option_order", option_preview=preview)
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="queued")
        return self._agent_review(rid, doc, rule_id, rule_type, condition, action,
                                  None, price=price, kind="option_order",
                                  option_preview=preview)

    def _dispatch_option_buy(self, doc: StrategyDoc, rule_id: str, condition: str,
                             spec: ActionSpec, price: Decimal,
                             reason: str) -> TriggerOutcome:
        underlying = doc.position.ticker
        right, _min_dte, _otm_pct, pick, early = self._pick_option_contract(
            doc, rule_id, condition, spec, price)
        if early is not None:
            return early

        intent = OptionIntent(underlying=underlying, right=right,
                              occ_symbol=pick.occ_symbol, side=OrderSide.BUY,
                              qty=pick.qty, est_premium=pick.est_premium,
                              reason=reason, strategy_id=doc.id)
        try:
            result = self.executor.execute_option(intent)
        except Exception as exc:  # noqa: BLE001 — mirror the stock AUTO+HARD
            # branch above: any executor failure must be reported and
            # notified, never crash the sentinel pass.
            self._notify_order(doc, pick.occ_symbol, "buy", False, str(exc))
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="error", detail=str(exc))
        if result.submitted:
            self._notify_order(doc, pick.occ_symbol, "buy", True, self._placed,
                               order=result.order)
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="executed", detail=self._placed)
        detail = "; ".join(result.decision.reasons)
        self._notify_order(doc, pick.occ_symbol, "buy", False, detail)
        return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                              disposition="rejected", detail=detail)

    def _dispatch_close_options(self, doc: StrategyDoc, rule_id: str, condition: str,
                                positions: dict[str, Position],
                                reason: str) -> TriggerOutcome:
        underlying = doc.position.ticker
        # Filter/parse/build/execute mechanics (including Finding 5's "build
        # the intent INSIDE the try" fix, so one bad position's fractional
        # qty never aborts the rest of the batch) now live in the shared
        # close_underlying_options (execution.py) -- ReviewQueue.
        # _run_option_close used to duplicate this whole block almost line
        # for line. This method keeps only its own presentation (the
        # "closed: .../errors: ..." detail text) and per-leg notifications,
        # unchanged from before the extraction.
        outcome = close_underlying_options(
            self.executor, underlying, positions.values(),
            reason=reason, strategy_id=doc.id)
        if not outcome.legs:
            self._notify_rule(doc, rule_id, condition, "skipped")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="no option positions to close")

        closed: list[str] = []
        issues: list[str] = []
        for leg in outcome.legs:
            ticker = leg.position.ticker
            if leg.error is not None:
                issues.append(f"{ticker}: {leg.error}")
                self._notify_order(doc, ticker, "sell", False, leg.error)
                continue
            if leg.result.submitted:
                closed.append(ticker)
                self._notify_order(doc, ticker, "sell", True, self._placed,
                                   order=leg.result.order)
            else:
                reasons = "; ".join(leg.result.decision.reasons)
                issues.append(f"{ticker}: {reasons}")
                self._notify_order(doc, ticker, "sell", False, reasons)

        detail = f"closed: {', '.join(closed)}" if closed else ""
        if issues:
            detail = (detail + "; " if detail else "") + f"errors: {'; '.join(issues)}"
        disposition = "error" if issues and not closed else "executed"
        return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                              disposition=disposition, detail=detail)

    def _agent_review(self, rid: int, doc: StrategyDoc, rule_id: str,
                      rule_type: RuleType, condition: str, action: str,
                      intent: OrderIntent | None, *, price: Decimal | None = None,
                      kind: str = "order", option_preview: str = "") -> TriggerOutcome:
        base = {"strategy_id": doc.id, "rule_id": rule_id}
        ticker = doc.position.ticker
        # Analysis phase: the review row is still pending here, so any
        # failure genuinely leaves it "queued" — the invariant holds.
        try:
            analysis = self.review_agent.analyze(dict(self.queue.get(rid)))
            self.queue.attach_analysis(rid, analysis.model_dump_json())
        except Exception as exc:  # noqa: BLE001 — a failed review must never lose the trigger
            self._notify_queued(doc, rid, ticker, action, "", price=price, intent=intent,
                                kind=kind, option_preview=option_preview)
            return TriggerOutcome(**base, disposition="queued",
                                  detail=f"agent review failed: {exc}")

        if analysis.reasoning.startswith("unparseable analysis:"):
            # The LLM's output couldn't be parsed as a recommendation at all —
            # this is not a genuine "skip" decision, so don't act on it.
            # Leave the trigger pending for human review.
            self._notify_queued(doc, rid, ticker, action, "", price=price, intent=intent,
                                kind=kind, option_preview=option_preview)
            return TriggerOutcome(
                **base, disposition="queued",
                detail="analysis unparseable — left for human review")

        # Option rules are always hard (never soft), so this branch never
        # actually sees kind == "option_order" today -- the `kind == "order"`
        # guard is defense in depth, not reliance on that invariant alone.
        autonomous = (doc.authorization == Authorization.AUTO
                      and rule_type == RuleType.SOFT and kind == "order")
        if not autonomous:
            recommendation = f"{analysis.recommendation} — {analysis.reasoning[:300]}"
            self._notify_queued(doc, rid, ticker, action, recommendation,
                                price=price, intent=intent, kind=kind,
                                option_preview=option_preview)
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
                       intent: OrderIntent | None = None, kind: str = "order",
                       option_preview: str = "") -> None:
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
            approve_url=approve_url, kind=kind, option_preview=option_preview)
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
