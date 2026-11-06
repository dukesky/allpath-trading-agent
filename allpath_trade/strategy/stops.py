"""Spent one-shot stops (ops 2026-11-06, item 6).

A rule without `rearm` fires once and then stays `triggered` until a
revision re-arms it (sentinel.py's rule loop only re-arms rules that set
`rearm`). Stop-losses are normally written one-shot, while dip-buy rules
re-arm, so after a stop sells the position the buys can rebuild it with
no armed stop behind it. On 2026-11-06 that was NVDA, TSM, MU and AMD on
paper, ~54% of equity, all trading below their spent stop levels.

Everything here is read-only visibility: nothing changes a rule's state.
Only SPENT stops are flagged (the strategy has stop rules and none is
armed). A strategy that never had a stop is a design choice, not this bug.
"""

from __future__ import annotations

import ast
from decimal import Decimal

from allpath_trade.broker.base import Position
from allpath_trade.strategy.actions import ActionError, ActionKind, parse_action
from allpath_trade.strategy.model import Rule, RuleState, StrategyDoc

_STOCK_SELLS = frozenset({ActionKind.SELL_ALL, ActionKind.SELL_PCT, ActionKind.SELL_VALUE})
_DOWNSIDE_NAMES = frozenset({"price", "pnl_pct"})


def _downside(condition: str) -> bool:
    """True when the condition has a `price`/`pnl_pct` upper bound, i.e. it
    fires on the way DOWN (`price < 205`, `205 > price`, `pnl_pct <= -8`)."""
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        terms = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            left, right = terms[i], terms[i + 1]
            if (isinstance(left, ast.Name) and left.id in _DOWNSIDE_NAMES
                    and isinstance(op, (ast.Lt, ast.LtE))):
                return True
            if (isinstance(right, ast.Name) and right.id in _DOWNSIDE_NAMES
                    and isinstance(op, (ast.Gt, ast.GtE))):
                return True
    return False


def is_stop_rule(rule: Rule) -> bool:
    """A stock sell that fires on a falling price or P&L."""
    try:
        kind = parse_action(rule.action).kind
    except ActionError:
        return False
    return kind in _STOCK_SELLS and _downside(rule.condition)


def spent_stop_rules(doc: StrategyDoc) -> list[Rule]:
    """The strategy's stop rules when it has some and NONE is armed; else []."""
    stops = [r for r in doc.rules if is_stop_rule(r)]
    if not stops or any(r.state == RuleState.ARMED for r in stops):
        return []
    return stops


def _describe(doc: StrategyDoc, rule: Rule, strategies) -> str:
    when = ""
    if strategies is not None:
        try:
            last = strategies.last_fire(doc.id, rule.id)
        except Exception:  # noqa: BLE001 — a missing date must not drop the warning
            last = None
        if last is not None:
            when = f" {last.date().isoformat()}"
    state = "spent" if rule.state == RuleState.TRIGGERED else rule.state.value
    return f"{doc.id}/{rule.id} ({rule.condition}), {state}{when}"


def stop_warning(doc: StrategyDoc, strategies=None) -> str:
    """One line for a BUY on `doc`: "" while a stop is armed (or the
    strategy never had one), else names the spent stop(s)."""
    spent = spent_stop_rules(doc)
    if not spent:
        return ""
    names = "; ".join(_describe(doc, r, strategies) for r in spent)
    return (f"No armed stop: {names}. This buy adds to a position the stop no "
            "longer protects; re-arm or revise the stop deliberately.")


def unprotected_positions(strategies, positions: list[Position],
                          equity: Decimal | None) -> list[dict]:
    """Held positions whose active strategies have stop rules but none
    armed. `strategies` is a StrategyStore (active strategies only)."""
    docs = strategies.load_all(errors=[])
    rows = []
    for p in positions:
        if p.qty == 0:
            continue
        mine = [d for d in docs if d.position.ticker == p.ticker]
        if any(r.state == RuleState.ARMED and is_stop_rule(r)
               for d in mine for r in d.rules):
            continue
        rules = [_describe(d, r, strategies) for d in mine for r in spent_stop_rules(d)]
        if not rules:
            continue
        weight = p.market_value / equity if equity and equity > 0 else None
        rows.append({"ticker": p.ticker, "qty": p.qty, "weight": weight,
                     "rules": rules})
    return rows


def no_armed_stop_line(strategies, positions: list[Position],
                       equity: Decimal | None) -> str:
    """The daily digest's line; "" when every stop-covered position still
    has an armed stop."""
    rows = unprotected_positions(strategies, positions, equity)
    if not rows:
        return ""
    parts = []
    for r in rows:
        weight = f" ({r['weight']:.1%})" if r["weight"] is not None else ""
        qty = format(r["qty"].normalize(), "f")
        parts.append(f"{r['ticker']} {qty} sh{weight}: {'; '.join(r['rules'])}")
    return (f"⚠ No armed stop on {len(rows)} held position(s): "
            + " | ".join(parts)
            + ". Re-arm or revise these stops deliberately; one already below "
            "its level fires at once when re-armed.")
