from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from allpath_trade.store.app_state import AppState
from allpath_trade.strategy.model import Authorization
from allpath_trade.strategy.store import StrategyStore


class BreakerTrip(BaseModel):
    peak: Decimal
    equity: Decimal
    drawdown: Decimal
    demoted: list[str]


class DrawdownBreaker:
    """Account-level kill-switch: when equity falls more than `halt_pct`
    below its recorded peak, demote every `auto` strategy to `confirm` --
    once -- and report the trip so the sentinel can alert. Peak and tripped
    state live in app_state (`drawdown_peak:{account}`,
    `drawdown_tripped:{account}`); recovery is a manual
    `allpath-trade breaker reset` after the user reviews the account."""

    def __init__(self, app_state: AppState, strategies: StrategyStore,
                 halt_pct: Decimal, account: str) -> None:
        self.app_state = app_state
        self.strategies = strategies
        self.halt_pct = halt_pct
        self.account = account
        self._peak_key = f"drawdown_peak:{account}"
        self._tripped_key = f"drawdown_tripped:{account}"

    def tripped_at(self) -> str | None:
        return self.app_state.get(self._tripped_key)

    def check(self, equity: Decimal) -> BreakerTrip | None:
        if self.halt_pct <= 0 or equity <= 0:
            return None
        if self.tripped_at() is not None:
            return None
        raw = self.app_state.get(self._peak_key)
        peak = Decimal(raw) if raw else equity
        if equity > peak:
            peak = equity
        self.app_state.set(self._peak_key, str(peak))
        drawdown = (peak - equity) / peak
        if drawdown <= self.halt_pct:
            return None
        demoted: list[str] = []
        errors: list[str] = []
        for doc in self.strategies.load_all(status=None, errors=errors):
            if doc.authorization != Authorization.AUTO:
                continue
            self.strategies.set_authorization(
                doc.id, Authorization.CONFIRM,
                f"drawdown breaker: {drawdown:.1%} below peak {peak}")
            demoted.append(doc.id)
        self.app_state.set(self._tripped_key,
                           datetime.now(UTC).isoformat())
        return BreakerTrip(peak=peak, equity=equity, drawdown=drawdown,
                           demoted=demoted)

    def reset(self) -> None:
        self.app_state.delete(self._peak_key)
        self.app_state.delete(self._tripped_key)
