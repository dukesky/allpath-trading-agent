"""Paper-study regime (spec 2026-10-25-regime-switch-design.md).

A regime is two switches flipped together: whether nightly reflection
revisions auto-apply, and whether the paper strategies trade on their own
authority. It lives in `app_state` under `regime:{account}` (value `human` or
`auto`) so it is read at the moment it is needed -- no restart -- and every
switch is appended to `regime_events`. `Settings.experiment_auto_apply_
revisions` (`.env`) remains only as the fallback for an account that has never
had a regime set.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from allpath_trade.config import Settings
from allpath_trade.store.app_state import AppState
from allpath_trade.strategy.model import Authorization, StrategyStatus
from allpath_trade.strategy.store import StrategyStore

REGIMES = ("human", "auto")
EVENTS_SHOWN = 10


class RegimeError(Exception):
    """A regime change was refused (bad value, wrong account, missing --by)."""


def regime_key(account: str) -> str:
    return f"regime:{account}"


def get_regime(app_state: AppState, account: str) -> str | None:
    value = app_state.get(regime_key(account))
    return value if value in REGIMES else None


def _fallback(settings: Settings, account: str) -> tuple[bool, str]:
    """Auto-apply when no regime is set: the shadow override (shadow only),
    else the global flag."""
    if account == "shadow" and settings.shadow_experiment_auto_apply_revisions is not None:
        return settings.shadow_experiment_auto_apply_revisions, "shadow setting"
    return settings.experiment_auto_apply_revisions, "global flag"


def _effective(app_state: AppState, settings: Settings, account: str) -> tuple[bool, str]:
    regime = get_regime(app_state, account)
    if regime is not None:
        return regime == "auto", "regime"
    return _fallback(settings, account)


def effective_auto_apply(app_state: AppState, settings: Settings, account: str) -> bool:
    """Whether reflection revisions auto-apply for `account`: its regime if
    set, else the shadow override (shadow only), else the global flag."""
    return _effective(app_state, settings, account)[0]


def regime_authorization(regime: str) -> Authorization:
    return Authorization.CONFIRM if regime == "human" else Authorization.AUTO


@dataclass
class RegimeStatus:
    account: str
    regime: str | None
    auto_apply: bool
    auto_apply_source: str  # "regime" | "shadow setting" | "global flag"
    authorization_counts: dict[str, int]  # active strategies only
    mismatches: list[str]  # active auto/confirm strategy ids != regime
    events: list[dict[str, Any]]  # newest first, at most EVENTS_SHOWN


def set_regime(*, app_state: AppState, conn: sqlite3.Connection,
               strategies: StrategyStore, settings: Settings, account: str,
               regime: str, by: str, note: str = "") -> RegimeStatus:
    if regime not in REGIMES:
        raise RegimeError(f"regime must be one of {', '.join(REGIMES)}; got {regime!r}")
    if account != "paper":
        raise RegimeError(
            "regime set is paper-only: shadow mirrors a real account, its orders "
            "are always advisory, and `auto` there would record ledger trades "
            "you never placed")
    by = by.strip()
    if not by:
        raise RegimeError("--by is required: say who is flipping the regime")

    target = regime_authorization(regime)
    app_state.set(regime_key(account), regime)
    flipped = 0
    for doc in strategies.load_all(status=None):
        if (doc.status == StrategyStatus.ACTIVE
                and doc.authorization in (Authorization.AUTO, Authorization.CONFIRM)
                and doc.authorization != target):
            strategies.set_authorization(
                doc.id, target, reason=f"regime {regime} (set by {by})")
            flipped += 1
    conn.execute(
        "INSERT INTO regime_events (ts, account, regime, by, note, strategies_flipped)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(UTC).isoformat(), account, regime, by, note, flipped))
    conn.commit()
    return regime_status(app_state=app_state, conn=conn, strategies=strategies,
                         settings=settings, account=account)


def regime_status(*, app_state: AppState, conn: sqlite3.Connection,
                  strategies: StrategyStore, settings: Settings,
                  account: str) -> RegimeStatus:
    regime = get_regime(app_state, account)
    auto_apply, source = _effective(app_state, settings, account)
    counts: dict[str, int] = {}
    mismatches: list[str] = []
    target = regime_authorization(regime) if regime else None
    for doc in strategies.load_all(status=StrategyStatus.ACTIVE):
        counts[doc.authorization.value] = counts.get(doc.authorization.value, 0) + 1
        if (target is not None
                and doc.authorization in (Authorization.AUTO, Authorization.CONFIRM)
                and doc.authorization != target):
            mismatches.append(doc.id)
    rows = conn.execute(
        "SELECT ts, account, regime, by, note, strategies_flipped FROM regime_events"
        " WHERE account = ? ORDER BY id DESC LIMIT ?",
        (account, EVENTS_SHOWN)).fetchall()
    return RegimeStatus(
        account=account, regime=regime, auto_apply=auto_apply,
        auto_apply_source=source, authorization_counts=counts,
        mismatches=mismatches, events=[dict(r) for r in rows])
