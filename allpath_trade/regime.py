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
from decimal import Decimal
from typing import Any

from allpath_trade.config import Settings
from allpath_trade.risk.breaker import DrawdownBreaker
from allpath_trade.store.app_state import AppState
from allpath_trade.store.db import LockedConnection
from allpath_trade.strategy.loader import parse_strategy_text
from allpath_trade.strategy.model import Authorization, StrategyStatus
from allpath_trade.strategy.store import StrategyStore

REGIMES = ("human", "auto")
EVENTS_SHOWN = 10


class RegimeError(Exception):
    """A regime change was refused (bad value, wrong account, missing --by)."""


def regime_key(account: str) -> str:
    return f"regime:{account}"


def get_regime(app_state: AppState, account: str) -> str | None:
    """The stored regime if it is exactly `human` or `auto`, else None. A
    present-but-invalid value also reads as None here -- callers that must
    fail safe use `invalid_regime_value` / `regime_demands_confirm`."""
    value = app_state.get(regime_key(account))
    return value if value in REGIMES else None


def invalid_regime_value(app_state: AppState, account: str) -> str | None:
    """A stored `regime:{account}` value that is present but not exactly
    `human`/`auto` (a hand edit, a typo) -- returned so it can be surfaced.
    Such a value is treated as `human` everywhere it matters (fail safe)."""
    value = app_state.get(regime_key(account))
    return value if value is not None and value not in REGIMES else None


def regime_demands_confirm(app_state: AppState, account: str) -> bool:
    """True when no paper strategy may trade on its own authority: the paper
    regime is `human`, or a stored value is present but invalid (fail safe).
    Always False for shadow (its orders are advisory; `regime set` refuses
    it) and with no regime stored. One app_state read."""
    if account != "paper":
        return False
    value = app_state.get(regime_key(account))
    return value is not None and value != "auto"


def _fallback(settings: Settings, account: str) -> tuple[bool, str]:
    """Auto-apply when no regime is set: the shadow override (shadow only),
    else the global flag."""
    if account == "shadow" and settings.shadow_experiment_auto_apply_revisions is not None:
        return settings.shadow_experiment_auto_apply_revisions, "shadow setting"
    return settings.experiment_auto_apply_revisions, "global flag"


INVALID_SOURCE = "invalid regime value (treated as human)"


def _effective(app_state: AppState, settings: Settings, account: str) -> tuple[bool, str]:
    # Shadow ignores any `regime:shadow` value: `regime set` refuses shadow,
    # so no regime_events row could ever back one (final review M3).
    if account == "shadow":
        return _fallback(settings, account)
    value = app_state.get(regime_key(account))
    if value in REGIMES:
        return value == "auto", "regime"
    if value is not None:
        return False, INVALID_SOURCE
    return _fallback(settings, account)


def effective_auto_apply(app_state: AppState, settings: Settings, account: str) -> bool:
    """Whether reflection revisions auto-apply for `account`. Paper: its
    regime if set (a present-but-invalid value counts as human, so False),
    else the global flag. Shadow: the shadow override, else the global flag
    -- a stored `regime:shadow` is ignored."""
    return _effective(app_state, settings, account)[0]


def follow_regime(store: StrategyStore, strategy_id: str) -> bool:
    """The demote-only regime correction shared by every writer that can
    put an `authorization: auto` paper strategy on disk outside
    `set_regime` -- the review applier, the web status route (activation),
    the terminal draft_strategy save. On the paper store, while the regime
    demands confirm (human, or an invalid stored value), an AUTO strategy is
    flipped to CONFIRM; returns True if it flipped.

    It never promotes CONFIRM -> AUTO: the drawdown breaker demotes to
    confirm, and an automatic promotion here would silently undo that kill
    switch on the next write (final review C1). Promotion happens only via
    an explicit `regime set auto`. NOTIFY and shadow are never touched; with
    no regime set this is a no-op. Reads the raw file (never the DB-merged
    doc), the same convention as `StrategyStore.set_authorization`."""
    if store._account != "paper":
        return False
    if not regime_demands_confirm(AppState(store._conn), "paper"):
        return False
    path = store.directory / f"{strategy_id}.yaml"
    current = parse_strategy_text(strategy_id, path.read_text())
    if current.authorization != Authorization.AUTO:
        return False
    store.set_authorization(strategy_id, Authorization.CONFIRM,
                            reason="follows regime human")
    return True


def regime_authorization(regime: str) -> Authorization:
    return Authorization.CONFIRM if regime == "human" else Authorization.AUTO


@dataclass
class RegimeStatus:
    account: str
    regime: str | None
    auto_apply: bool
    auto_apply_source: str  # "regime" | "shadow setting" | "global flag" | INVALID_SOURCE
    authorization_counts: dict[str, int]  # non-archived strategies
    mismatches: list[str]  # non-archived auto/confirm strategy ids != regime
    events: list[dict[str, Any]]  # newest first, at most EVENTS_SHOWN
    # A stored value that is not exactly human/auto, surfaced so the CLI and
    # the nav chip can warn; it is treated as human (fail safe).
    invalid_value: str | None = None


BREAKER_TRIPPED_MESSAGE = ("the paper drawdown breaker is tripped — reset it "
                           "first (allpath-trade breaker reset)")


def _flippable(doc) -> bool:
    return (doc.status != StrategyStatus.ARCHIVED
            and doc.authorization in (Authorization.AUTO, Authorization.CONFIRM))


def set_regime(*, app_state: AppState, conn: LockedConnection,
               strategies: StrategyStore, settings: Settings, account: str,
               regime: str, by: str, note: str = "") -> RegimeStatus:
    """Switch the paper regime. Order matters (final review I2):

    1. Refuse `auto` while the paper drawdown breaker is tripped -- before
       any write, so a refusal changes nothing.
    2. Flip the strategy files: every NON-ARCHIVED (active, paused, draft)
       auto/confirm strategy whose authorization differs from the target.
       Idempotent, so a crash here is fixed by re-running the command.
    3. Write the regime value and its `regime_events` row in ONE
       transaction, so the logged regime and the effective one can never
       disagree. A failure here leaves the stored value unchanged."""
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
    if regime == "auto":
        # The breaker's own key/convention (risk/breaker.py); halt_pct is
        # irrelevant to `tripped_at`.
        breaker = DrawdownBreaker(app_state, strategies, halt_pct=Decimal(0),
                                  account=account)
        if breaker.tripped_at() is not None:
            raise RegimeError(BREAKER_TRIPPED_MESSAGE)

    target = regime_authorization(regime)
    flipped = 0
    for doc in strategies.load_all(status=None):
        if _flippable(doc) and doc.authorization != target:
            strategies.set_authorization(
                doc.id, target, reason=f"regime {regime} (set by {by})")
            flipped += 1
    try:
        with conn.transaction():
            # Raw upsert, not AppState.set: that commits on its own, which
            # would end this transaction early.
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (regime_key(account), regime))
            conn.execute(
                "INSERT INTO regime_events (ts, account, regime, by, note,"
                " strategies_flipped) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), account, regime, by, note, flipped))
    except sqlite3.Error as exc:
        raise RegimeError(
            f"could not record the regime switch ({exc}); the regime is "
            f"unchanged. {flipped} strategy file(s) were already flipped -- "
            "re-run the command once the database is fixed") from exc
    return regime_status(app_state=app_state, conn=conn, strategies=strategies,
                         settings=settings, account=account)


def regime_status(*, app_state: AppState, conn: sqlite3.Connection,
                  strategies: StrategyStore, settings: Settings,
                  account: str) -> RegimeStatus:
    regime = get_regime(app_state, account)
    invalid = invalid_regime_value(app_state, account)
    auto_apply, source = _effective(app_state, settings, account)
    counts: dict[str, int] = {}
    mismatches: list[str] = []
    target = regime_authorization(regime) if regime else None
    for doc in strategies.load_all(status=None):
        if doc.status == StrategyStatus.ARCHIVED:
            continue
        counts[doc.authorization.value] = counts.get(doc.authorization.value, 0) + 1
        if target is not None and _flippable(doc) and doc.authorization != target:
            mismatches.append(doc.id)
    rows = conn.execute(
        "SELECT ts, account, regime, by, note, strategies_flipped FROM regime_events"
        " WHERE account = ? ORDER BY id DESC LIMIT ?",
        (account, EVENTS_SHOWN)).fetchall()
    return RegimeStatus(
        account=account, regime=regime, auto_apply=auto_apply,
        auto_apply_source=source, authorization_counts=counts,
        mismatches=mismatches, events=[dict(r) for r in rows],
        invalid_value=invalid)
