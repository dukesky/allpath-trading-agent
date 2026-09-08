from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from allpath_trade.broker.base import (
    Account,
    OptionIntent,
    OrderIntent,
    OrderSide,
    Position,
    parse_occ_symbol,
)


class RiskLimits(BaseModel):
    max_order_value: Decimal = Decimal(5000)
    max_position_weight: Decimal = Decimal("0.25")  # fraction of equity
    max_options_weight: Decimal = Decimal("0.10")  # total option exposure vs equity
    max_daily_trades: int = 10
    min_cash_reserve: Decimal = Decimal(0)
    allow_live: bool = False


# The account whose ceilings are the bare settings themselves; it has no
# prefixed overrides (a `paper_max_order_value` setting does not exist).
DEFAULT_LIMIT_ACCOUNT = "paper"


def limits_for_account(settings: object, account: str) -> RiskLimits:
    """The risk ceilings that apply to ONE account.

    Every field falls back to the bare setting (`max_order_value`, ...),
    which is what the paper account and any future account use. The shadow
    account additionally honours a `shadow_`-prefixed override per field,
    because it mirrors a real brokerage whose size and concentration are
    nothing like the paper sandbox's -- see `Settings`' own comment for why
    one shared ceiling cannot serve both.

    `None` on an override means "not set" and falls through to the base
    value; it deliberately does NOT mean 0, so an operator can leave any
    subset overridden. `settings` is duck-typed rather than imported as
    `Settings` to keep `risk/` free of a dependency on the config layer.
    """
    fields = ("max_order_value", "max_position_weight", "max_options_weight",
              "max_daily_trades", "min_cash_reserve")
    values = {}
    for name in fields:
        base = getattr(settings, name, None)
        override = (getattr(settings, f"{account}_{name}", None)
                    if account != DEFAULT_LIMIT_ACCOUNT else None)
        chosen = override if override is not None else base
        if chosen is not None:
            values[name] = chosen
    return RiskLimits(**values)


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = []


class RiskGate:
    """Deterministic pre-trade checks. Every order intent passes through here;
    there is no code path from the LLM to a broker that skips this gate."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def check(self, intent: OrderIntent, *, account: Account,
              positions: list[Position], trades_today: int,
              is_paper: bool, price: Decimal) -> RiskDecision:
        reasons: list[str] = []
        lim = self.limits
        order_value = intent.notional if intent.notional is not None else intent.qty * price
        pos = next((p for p in positions if p.ticker == intent.ticker), None)

        if not is_paper and not lim.allow_live:
            reasons.append("live trading is disabled (allow_live=false)")

        if order_value > lim.max_order_value:
            reasons.append(
                f"order value {order_value} exceeds max_order_value {lim.max_order_value}")

        if trades_today >= lim.max_daily_trades:
            reasons.append(
                f"daily trade limit reached ({trades_today}/{lim.max_daily_trades})")

        if intent.side == OrderSide.BUY:
            current = pos.market_value if pos else Decimal(0)
            if account.equity > 0:
                weight = (current + order_value) / account.equity
                if weight > lim.max_position_weight:
                    reasons.append(
                        f"resulting position weight {weight:.2%} exceeds "
                        f"max_position_weight {lim.max_position_weight:.0%}")
            if account.cash - order_value < lim.min_cash_reserve:
                reasons.append(
                    f"buy would violate cash reserve minimum {lim.min_cash_reserve}")
        else:  # SELL — no shorting in v1
            held_qty = pos.qty if pos else Decimal(0)
            held_value = pos.market_value if pos else Decimal(0)
            if intent.qty is not None and intent.qty > held_qty:
                reasons.append(
                    f"sell qty {intent.qty} exceeds position ({held_qty} held)")
            if intent.notional is not None and intent.notional > held_value:
                reasons.append(
                    f"sell notional {intent.notional} exceeds position value {held_value}")

        return RiskDecision(approved=not reasons, reasons=reasons)

    def check_option(self, intent: OptionIntent, *, account: Account,
                      positions: list[Position], trades_today: int,
                      is_paper: bool) -> RiskDecision:
        """Pre-trade checks for a single-leg option order. `est_premium` is
        already a total-dollar figure (ask*100*qty), so unlike `check` there
        is no separate `price` param to convert qty into dollars.

        SELL (close) intents are exempt from every cap below -- the premium
        cap, the exposure cap, the cash-reserve check, and (Finding 2) the
        daily-trade cap too: a close can only shrink existing option
        exposure, never grow risk, and both the DTE<=1 expiry safety sweep
        and a close_options stop-loss rule execute through this exact SELL
        path. A safety sweep or stop-loss exit must never be blocked by a
        cap shared with every BUY (stock or option) that already ran
        earlier in the day -- that would starve the one thing meant to run
        no matter what."""
        reasons: list[str] = []
        lim = self.limits

        if not is_paper and not lim.allow_live:
            reasons.append("live trading is disabled (allow_live=false)")

        if intent.side == OrderSide.BUY:
            if intent.est_premium > lim.max_order_value:
                reasons.append(
                    f"order value {intent.est_premium} exceeds max_order_value "
                    f"{lim.max_order_value}")

            if account.equity > 0:
                existing_exposure = sum(
                    (abs(p.market_value) for p in positions if parse_occ_symbol(p.ticker)),
                    Decimal(0),
                )
                exposure = existing_exposure + intent.est_premium
                max_allowed = lim.max_options_weight * account.equity
                if exposure > max_allowed:
                    reasons.append(
                        f"options exposure {exposure} exceeds max_options_weight "
                        f"{lim.max_options_weight:.0%} of equity ({max_allowed})")

            # Finding 3: mirrors `check`'s own cash-reserve floor for stock
            # buys -- an option buy spends real cash (the premium) just like
            # a stock buy does, and had no equivalent check at all before
            # this, letting a premium blow straight through min_cash_reserve.
            if account.cash - intent.est_premium < lim.min_cash_reserve:
                reasons.append(
                    f"buy would violate cash reserve minimum {lim.min_cash_reserve}")

            # Finding 2: daily-trade cap applies to BUYS only -- see the
            # docstring above for why SELL (close) is fully exempt.
            if trades_today >= lim.max_daily_trades:
                reasons.append(
                    f"daily trade limit reached ({trades_today}/{lim.max_daily_trades})")

        return RiskDecision(approved=not reasons, reasons=reasons)
