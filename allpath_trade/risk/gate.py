from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from allpath_trade.broker.base import Account, OrderIntent, OrderSide, Position


class RiskLimits(BaseModel):
    max_order_value: Decimal = Decimal(5000)
    max_position_weight: Decimal = Decimal("0.25")  # fraction of equity
    max_options_weight: Decimal = Decimal("0.10")  # total option exposure vs equity
    max_daily_trades: int = 10
    min_cash_reserve: Decimal = Decimal(0)
    allow_live: bool = False


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
