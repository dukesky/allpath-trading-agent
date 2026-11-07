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


class BreakerDeepening(BaseModel):
    """Equity fell another `DEEPENING_STEP` below the frozen peak while the
    breaker was already tripped (ops 2026-11-06: tripped breakers went
    silent however far equity fell)."""
    peak: Decimal
    equity: Decimal
    drawdown: Decimal
    level: Decimal          # the step just crossed, e.g. 0.20, 0.25, ...
    tripped_at: str


# Further alerts while tripped: one per additional 5 percentage points of
# drawdown below the frozen peak (halt 15% -> 20%, 25%, 30%, ...).
DEEPENING_STEP = Decimal("0.05")


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
        self._alert_level_key = f"drawdown_alert_level:{account}"

    def tripped_at(self) -> str | None:
        return self.app_state.get(self._tripped_key)

    def peak(self) -> Decimal | None:
        """The recorded equity peak, None before the first check or after
        a reset (read by the shadow check-in's live facts)."""
        raw = self.app_state.get(self._peak_key)
        return Decimal(raw) if raw else None

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
        # The step this trip already alerted for, so `deepening` only
        # speaks up past it (a gap-down trip at 22% next alerts at 25%).
        self.app_state.set(self._alert_level_key, str(self._step_level(drawdown)))
        return BreakerTrip(peak=peak, equity=equity, drawdown=drawdown,
                           demoted=demoted)

    def _step_level(self, drawdown: Decimal) -> Decimal:
        """The deepest `halt_pct + k * DEEPENING_STEP` (k >= 0) at or above
        which `drawdown` sits."""
        steps = max(0, int((drawdown - self.halt_pct) / DEEPENING_STEP))
        return self.halt_pct + steps * DEEPENING_STEP

    def deepening(self, equity: Decimal) -> BreakerDeepening | None:
        """While tripped, report once each further `DEEPENING_STEP` that
        equity falls below the frozen peak. Alert-only: it never demotes,
        blocks, or moves the peak (what a trip does stays in `check`)."""
        if self.halt_pct <= 0 or equity <= 0:
            return None
        tripped_at = self.tripped_at()
        raw_peak = self.app_state.get(self._peak_key)
        if tripped_at is None or not raw_peak:
            return None
        peak = Decimal(raw_peak)
        drawdown = (peak - equity) / peak
        raw_level = self.app_state.get(self._alert_level_key)
        alerted = Decimal(raw_level) if raw_level else self.halt_pct
        level = self._step_level(drawdown)
        if level <= alerted:
            return None
        self.app_state.set(self._alert_level_key, str(level))
        return BreakerDeepening(peak=peak, equity=equity, drawdown=drawdown,
                                level=level, tripped_at=tripped_at)

    def reset(self) -> None:
        self.app_state.delete(self._peak_key)
        self.app_state.delete(self._tripped_key)
        self.app_state.delete(self._alert_level_key)
