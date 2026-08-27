from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel

from allpath_trade.broker.base import OrderIntent, OrderSide, Position
from allpath_trade.strategy.model import StrategyDoc


class ActionKind(str, Enum):
    SELL_PCT = "sell_pct"
    SELL_ALL = "sell_all"
    SELL_VALUE = "sell_value"
    BUY_VALUE = "buy_value"
    BUY_TO_TARGET = "buy_to_target"
    BUY_CALL = "buy_call"
    BUY_PUT = "buy_put"
    CLOSE_OPTIONS = "close_options"


_OPTION_KINDS = frozenset({ActionKind.BUY_CALL, ActionKind.BUY_PUT, ActionKind.CLOSE_OPTIONS})


class ActionSpec(BaseModel):
    kind: ActionKind
    amount: Decimal | None = None
    # Option-only params. Left None when the rule text omits them -- the
    # sentinel call site (not this parser) applies the v1 defaults
    # (dte 7, otm 2%) at execution time.
    min_dte: int | None = None
    otm_pct: Decimal | None = None  # fraction, e.g. Decimal("0.03") for 3%


class ActionError(Exception):
    pass


def is_option_action(spec: ActionSpec) -> bool:
    return spec.kind in _OPTION_KINDS


_PATTERNS: list[tuple[re.Pattern[str], ActionKind]] = [
    (re.compile(r"^sell\s+all$", re.IGNORECASE), ActionKind.SELL_ALL),
    (re.compile(r"^sell\s+(?P<num>[\d,.]+)%$", re.IGNORECASE), ActionKind.SELL_PCT),
    (re.compile(r"^sell\s+\$(?P<num>[\d,.]+)$", re.IGNORECASE), ActionKind.SELL_VALUE),
    (re.compile(r"^buy\s+\$(?P<num>[\d,.]+)$", re.IGNORECASE), ActionKind.BUY_VALUE),
    (re.compile(r"^buy\s+to\s+target_weight$", re.IGNORECASE), ActionKind.BUY_TO_TARGET),
    (re.compile(r"^buy_(?P<right>call|put)\s+\$(?P<num>[\d,.]+)"
                r"(?:\s+dte>=(?P<dte>\d+))?(?:\s+otm=(?P<otm>[\d.]+)%)?$",
                re.IGNORECASE), ActionKind.BUY_CALL),  # right group decides CALL/PUT
    (re.compile(r"^close_options$", re.IGNORECASE), ActionKind.CLOSE_OPTIONS),
]


def parse_action(text: str) -> ActionSpec:
    stripped = text.strip()
    for pattern, kind in _PATTERNS:
        m = pattern.match(stripped)
        if not m:
            continue
        gd = m.groupdict()
        if gd.get("right") is not None:
            kind = ActionKind.BUY_CALL if gd["right"].lower() == "call" else ActionKind.BUY_PUT
        amount: Decimal | None = None
        if gd.get("num") is not None:
            try:
                amount = Decimal(gd["num"].replace(",", ""))
            except InvalidOperation:
                raise ActionError(f"invalid amount in action: {text!r}")
            if amount <= 0:
                raise ActionError(f"amount must be positive: {text!r}")
            if kind == ActionKind.SELL_PCT and amount > 100:
                raise ActionError(f"sell percent > 100: {text!r}")
        min_dte: int | None = None
        if gd.get("dte") is not None:
            min_dte = int(gd["dte"])
            if min_dte < 0:
                raise ActionError(f"dte must be >= 0: {text!r}")
        otm_pct: Decimal | None = None
        if gd.get("otm") is not None:
            try:
                otm_raw = Decimal(gd["otm"])
            except InvalidOperation:
                raise ActionError(f"invalid otm in action: {text!r}")
            if not (0 < otm_raw <= 50):
                raise ActionError(f"otm must be in (0, 50]%: {text!r}")
            otm_pct = otm_raw / Decimal(100)
        return ActionSpec(kind=kind, amount=amount, min_dte=min_dte, otm_pct=otm_pct)
    raise ActionError(f"unrecognized action: {text!r}")


def to_order_intent(spec: ActionSpec, *, strategy: StrategyDoc, rule_id: str,
                    price: Decimal, position: Position | None, equity: Decimal,
                    reason: str) -> OrderIntent | None:
    if is_option_action(spec):
        # Defensive: option kinds must be routed to the options execution
        # path (the sentinel, Task 6) before ever reaching here -- this
        # function only knows how to build equity OrderIntents. A future
        # call-site mistake should fail loudly instead of silently mis-
        # ordering an option leg as a stock trade.
        raise ActionError(f"to_order_intent cannot handle option action {spec.kind!r}; "
                          f"option actions must be routed before this call")
    ticker = strategy.position.ticker
    held_qty = position.qty if position else Decimal(0)

    if spec.kind in (ActionKind.SELL_ALL, ActionKind.SELL_PCT, ActionKind.SELL_VALUE):
        if held_qty <= 0:
            return None
        if spec.kind == ActionKind.SELL_ALL:
            return OrderIntent(ticker=ticker, side=OrderSide.SELL, qty=held_qty,
                               reason=reason, strategy_id=strategy.id)
        if spec.kind == ActionKind.SELL_PCT:
            qty = (held_qty * spec.amount / Decimal(100)).quantize(Decimal("0.0001"))
            if qty <= 0:
                return None
            return OrderIntent(ticker=ticker, side=OrderSide.SELL, qty=qty,
                               reason=reason, strategy_id=strategy.id)
        return OrderIntent(ticker=ticker, side=OrderSide.SELL, notional=spec.amount,
                           reason=reason, strategy_id=strategy.id)

    if spec.kind == ActionKind.BUY_VALUE:
        return OrderIntent(ticker=ticker, side=OrderSide.BUY, notional=spec.amount,
                           reason=reason, strategy_id=strategy.id)

    # BUY_TO_TARGET
    plan = strategy.position
    target_value = (plan.target_value if plan.target_value is not None
                    else plan.target_weight * equity)
    current_value = held_qty * price
    gap = (target_value - current_value).quantize(Decimal("0.01"))
    if gap <= 0:
        return None
    return OrderIntent(ticker=ticker, side=OrderSide.BUY, notional=gap,
                       reason=reason, strategy_id=strategy.id)
