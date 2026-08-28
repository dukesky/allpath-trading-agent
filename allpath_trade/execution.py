from __future__ import annotations

import concurrent.futures
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from allpath_trade.broker.base import (
    Broker,
    OptionIntent,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
)
from allpath_trade.broker.options_mcp import OptionsBackend
from allpath_trade.data.base import DataSource
from allpath_trade.risk.gate import RiskDecision, RiskGate
from allpath_trade.store.journal import TradeJournal

# Option-order statuses collapsed onto our coarse OrderStatus, mirroring
# broker/alpaca.py's _STATUS_MAP -- the MCP server's place_option_order
# payload carries the same Alpaca order-status vocabulary (see
# broker/options_mcp.py's module docstring). Kept as its own local copy
# rather than importing alpaca.py's map: that map is a private module
# constant of a different broker implementation, and duplicating six
# string literals here is cheaper than reaching across broker modules for
# it.
_OPTION_STATUS_MAP = {
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


def _parse_payload_datetime(value: object) -> datetime | None:
    """Best-effort parse of a datetime the MCP payload may or may not carry
    in a usable form. Anything not a parseable ISO string (missing, wrong
    type, malformed) yields None rather than raising -- the caller falls
    back to `datetime.now(UTC)` for `submitted_at`, or leaves `filled_at`
    unset."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _synthetic_order_intent(intent: OptionIntent) -> OrderIntent:
    """The journal (and the digest/reflection pipelines built on it) only
    know how to read `OrderIntent` rows -- this projects an `OptionIntent`
    onto that shape with zero schema change, per spec §5. `notional` is
    deliberately left None: the gate's own decision reasons already carry
    the premium figure, and `notional` on a real OrderIntent means "spend
    this many dollars of stock", which contracts are not."""
    return OrderIntent(
        ticker=intent.occ_symbol,
        side=intent.side,
        qty=Decimal(intent.qty),
        notional=None,
        reason=intent.reason,
        strategy_id=intent.strategy_id,
    )


def _order_from_payload(payload: dict, intent: OptionIntent) -> Order:
    """Build an `Order` defensively from the MCP server's raw `data`
    envelope for `place_option_order`. Only `id` and `status` are
    documented as always present (see options_mcp.py's module docstring);
    everything else falls back to a safe default rather than raising on a
    field the server happens to omit."""
    status = _OPTION_STATUS_MAP.get(str(payload.get("status", "")).lower(), OrderStatus.SUBMITTED)

    qty_raw = payload.get("qty")
    qty = Decimal(str(qty_raw)) if qty_raw not in (None, "") else Decimal(intent.qty)

    filled_qty_raw = payload.get("filled_qty")
    filled_qty_present = filled_qty_raw not in (None, "")
    filled_qty = Decimal(str(filled_qty_raw)) if filled_qty_present else Decimal(0)

    filled_avg_price_raw = payload.get("filled_avg_price")
    filled_avg_price = (Decimal(str(filled_avg_price_raw))
                        if filled_avg_price_raw not in (None, "") else None)

    # I2 (reviewer, task-5 fix round): a payload reporting status "filled"
    # but with no usable filled_qty (missing/differently-named field) would
    # otherwise journal a self-contradictory row -- filled_qty=0 alongside
    # status=filled. Degrade to SUBMITTED instead of fabricating a filled
    # row: this is the same honest-uncertainty stance TradeJournal.refresh_
    # fill and the ongoing refresh_pending_fills sweep already take for a
    # not-yet-confirmed fill (NULL fill columns, status=submitted) -- a
    # later sweep or manual reconciliation can still correct it once the
    # real fill data is available, whereas a fabricated FILLED/0 row would
    # just sit there wrong.
    if status == OrderStatus.FILLED and not filled_qty_present:
        status = OrderStatus.SUBMITTED

    submitted_at = _parse_payload_datetime(payload.get("submitted_at")) or datetime.now(UTC)
    filled_at = _parse_payload_datetime(payload.get("filled_at"))

    return Order(
        id=str(payload.get("id", "")),
        ticker=intent.occ_symbol,
        side=intent.side,
        qty=qty,
        notional=None,
        status=status,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        submitted_at=submitted_at,
        filled_at=filled_at,
    )


class ExecutionError(Exception):
    pass


# Per-order-poll bound (I4). alpaca-py's TradingClient passes no timeout to
# the underlying `requests` call (verified against the SDK) and retries
# 429/504 responses 3x with 3s sleeps between attempts -- a single
# get_order() can legitimately block for tens of seconds. refresh_pending_
# fills runs on the scheduler thread, on every sentinel pass,
# unconditionally (market open or closed -- see scheduler._run_sentinel_
# pass), so an unbounded call here stalls the heartbeat and every strategy
# evaluation behind it, 24/7.
#
# Mirrors web/models_catalog.py's `_fetch_pool` / `future.result(timeout=
# ...)` pattern exactly: run the blocking call on a worker thread and
# enforce a true wall-clock deadline with `.result(timeout=...)`, since the
# call itself has no reliable timeout of its own. Not the dashboard's
# `_broker_pool` (allpath_trade.web.routes.dashboard) -- that pool is sized
# and owned by an unrelated route module, and reusing it here would couple
# two independently-owned modules together for one poll call.
#
# Caveat inherited from models_catalog.py: Python cannot cancel a running
# thread. A poll that times out keeps running in this pool's single worker
# until it eventually returns (or the process exits); anything queued
# behind it waits. That's an accepted tradeoff here too -- the caller (this
# sweep, and in turn the scheduler thread) is unblocked either way, which is
# the actual goal; a stuck poll degrading its own row via the per-row
# try/except below is preferable to it stalling the whole pass.
_ORDER_POLL_TIMEOUT_SECONDS = 10

_poll_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="fill-refresh-poll")

# Whole-sweep wall-clock bound (I4). Capping each individual poll at
# _ORDER_POLL_TIMEOUT_SECONDS still allows a worst case of
# unfilled_recent's row count (20) x 10s = 200s for one sweep if the broker
# is merely slow rather than fully hung -- far too long for something
# running unconditionally on every sentinel tick. This deadline, checked
# with time.monotonic() before each row, cuts a slow-but-not-hung sweep off
# after ~20s: whatever rows didn't get polled this pass simply wait for the
# next one, same as rows that never made it into this pass's LIMIT-20
# selection (see TradeJournal.unfilled_recent).
_SWEEP_DEADLINE_SECONDS = 20


# Home: here rather than store/journal.py, because this is a broker-polling
# concern (Broker.get_order round trips), not a storage concern -- journal.py
# stays pure persistence, matching how Executor.execute already keeps its own
# post-submit poll (see the comment there) in this module rather than in the
# journal. Called from scheduler._run_sentinel_pass at the start of every
# sentinel pass so a DAY order queued outside market hours (see Order.filled_at)
# gets its fill recorded within one sentinel interval of actually filling,
# instead of staying "submitted" forever.
def refresh_pending_fills(journal: TradeJournal, broker: Broker) -> None:
    """Re-poll still-unresolved trades and write back whatever Alpaca
    reports -- a fill, a partial fill, or a terminal non-fill status
    (canceled/expired/rejected; see TradeJournal.refresh_fill).

    Row selection and its cap live in TradeJournal.unfilled_recent (20 most
    recent unresolved rows per pass, no age cutoff -- see its docstring for
    why). Each row's get_order round trip is individually bounded by
    _ORDER_POLL_TIMEOUT_SECONDS (via _poll_pool), and the whole sweep is
    bounded by _SWEEP_DEADLINE_SECONDS: once the deadline passes, remaining
    rows are left for the next sentinel pass rather than processed now.

    Each row polls in its own try/except -- a broker outage, a timed-out
    poll, or one bad order id must degrade that single row, not the rest of
    the batch or the sentinel pass calling this. Failures are counted and
    reported as a single stderr line per sweep (M1) rather than per row, so
    a broker outage doesn't spam the log once per stuck order."""
    rows = journal.unfilled_recent()
    deadline = time.monotonic() + _SWEEP_DEADLINE_SECONDS
    attempted = 0
    failures = 0
    for row in rows:
        if time.monotonic() >= deadline:
            break
        attempted += 1
        try:
            order = _poll_pool.submit(broker.get_order, row["broker_order_id"]).result(
                timeout=_ORDER_POLL_TIMEOUT_SECONDS)
            journal.refresh_fill(row["id"], order)
        except Exception:  # noqa: BLE001 — one bad/slow row must not break the sweep
            failures += 1
    if failures:
        print(f"[fill-refresh] {failures} of {attempted} refreshes failed", file=sys.stderr)


class ExecutionResult(BaseModel):
    submitted: bool
    order: Order | None
    decision: RiskDecision


class Executor:
    """The single entry point for trading. Everything above (scheduler,
    agent tools) creates OrderIntents and calls execute()."""

    def __init__(self, broker: Broker, gate: RiskGate,
                 journal: TradeJournal, data: DataSource,
                 options_backend: OptionsBackend | None = None) -> None:
        self.broker = broker
        self.gate = gate
        self.journal = journal
        self.data = data
        self.options_backend = options_backend

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

        trade_id = self.journal.record(intent, decision, order)
        if order.status != OrderStatus.FILLED:
            # Market orders often finish filling within the same round trip
            # that submitted them, so one extra poll right here catches the
            # common case and gives the journal (and later, the reflection
            # briefing) a real fill price instead of "submitted". If the
            # poll itself fails, leave the as-submitted row alone rather
            # than retrying or raising: NULL fill columns honestly say "we
            # don't know yet" and a later reconciliation pass can fill them
            # in, but this is not the place to build a polling loop.
            try:
                refreshed = self.broker.get_order(order.id)
                if refreshed is not None:
                    self.journal.refresh_fill(trade_id, refreshed)
            except Exception:  # noqa: BLE001, S110 — poll + write degrade together
                pass
        return ExecutionResult(submitted=True, order=order, decision=decision)

    def execute_option(self, intent: OptionIntent) -> ExecutionResult:
        """Option-order counterpart to `execute`. `est_premium` is already a
        total-dollar figure, so unlike `execute` there is no quote to fetch
        here at all -- an OCC symbol like "META260918C00600000" would break
        `self.data`'s stock-quote lookup (yfinance) anyway."""
        order_intent = _synthetic_order_intent(intent)

        try:
            account = self.broker.get_account()
            positions = self.broker.get_positions()
            trades_today = self.journal.trades_today()
        except Exception as exc:
            failed = RiskDecision(approved=False,
                                  reasons=[f"data error: {exc}"])
            self.journal.record(order_intent, failed, None, status_override="error")
            raise ExecutionError(str(exc)) from exc

        decision = self.gate.check_option(
            intent,
            account=account,
            positions=positions,
            trades_today=trades_today,
            is_paper=self.broker.is_paper,
        )
        if not decision.approved:
            self.journal.record(order_intent, decision, None)
            return ExecutionResult(submitted=False, order=None, decision=decision)

        if self.options_backend is None:
            failed = RiskDecision(approved=False, reasons=["options trading disabled"])
            self.journal.record(order_intent, failed, None, status_override="error")
            raise ExecutionError("options trading disabled")

        position_intent = "buy_to_open" if intent.side == OrderSide.BUY else "sell_to_close"
        side = "buy" if intent.side == OrderSide.BUY else "sell"
        try:
            payload = self.options_backend.place_option_order(
                intent.occ_symbol, side, intent.qty, position_intent)
        except Exception as exc:
            failed = RiskDecision(approved=False,
                                  reasons=[f"broker error: {exc}"])
            self.journal.record(order_intent, failed, None, status_override="error")
            raise ExecutionError(str(exc)) from exc

        try:
            order = _order_from_payload(payload, intent)
        except Exception as exc:
            # The broker call above already succeeded -- a real order was
            # placed -- so a parse failure here (e.g. a present-but-
            # malformed numeric field like filled_qty="N/A") must not leave
            # that trade with zero journal record: it would silently
            # undercount trades_today and options exposure on every check
            # after this one. Journal what we know (the order WAS placed,
            # we just can't read the response) and raise, exactly like the
            # broker-error path above.
            failed = RiskDecision(
                approved=False,
                reasons=[f"order placed but response unparseable: {exc}"])
            self.journal.record(order_intent, failed, None, status_override="error")
            raise ExecutionError(f"order placed but response unparseable: {exc}") from exc

        if not payload.get("id"):
            # Incident 2026-08-28: five option entries dispatched at 1:32am
            # ET (market closed) all "succeeded" -- Alpaca's options venue
            # doesn't accept orders outside regular hours, but the MCP
            # server still returns a 200-shaped `data` envelope for the
            # rejection, just one with no `id` because no order was ever
            # created. `_order_from_payload` defaults a missing `id` to ""
            # rather than raising (see its own docstring, and
            # docs/TODO.md's now-resolved entry on the id-less case), so
            # without this check that payload sails straight through to the
            # "order placed" path below and gets journaled as an ordinary
            # "submitted" row with broker_order_id="" -- a phantom order
            # that silently burns whatever one-shot rule dispatched it, with
            # nobody notified. Treat it exactly like the broker-error path
            # above: no "submitted" row is ever written for it, and
            # order=None here means broker_order_id lands NULL (not ""),
            # keeping it out of TradeJournal.unfilled_recent's endless
            # re-poll too.
            snippet = json.dumps(payload, default=str)[:200]
            reason = (
                "order not created (no order id in broker response; "
                f"market closed?): {snippet}")
            failed = RiskDecision(approved=False, reasons=[reason])
            self.journal.record(order_intent, failed, None, status_override="error")
            raise ExecutionError(reason)

        self.journal.record(order_intent, decision, order)
        return ExecutionResult(submitted=True, order=order, decision=decision)
