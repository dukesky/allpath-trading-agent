"""TDSC R2 (dev-requests item 3): counterfactual rule replay on daily bars.

Targets, from `pending_reviews` strategy revisions:
- vetoed proposal: each rule it would have added or modified, as proposed
  (`variant=proposed`); for a vetoed modify/remove also the original rule the
  veto kept (`variant=kept`), replayed the same way so the two compare on
  equal footing;
- applied removal: the rule as it was before removal (`variant=removed`).

Each rule's condition is evaluated with the sentinel's own evaluator
(`strategy.conditions`) on raw daily bars from the first trading day after
the decision's ET date up to `asof`. Daily bars are coarser than the
sentinel's intraday ticks:
- `fire_basis=close`: the day's close satisfies the condition;
- `fire_basis=intraday`: only some price inside [low, high] does (found
  exactly by testing the condition's own thresholds), so the hourly sentinel
  may or may not have caught it.
Position terms (position_weight, position_qty, target_weight,
avg_entry_price, pnl_pct) cannot be replayed from bars: in an AND they are
dropped (assumed true) and the row says `position_terms_relaxed=1`; under
OR/NOT the row is `unevaluable`.

Hypothetical fill = the fire day's close. Marks at +5 and +20 trading days and
at `asof`. `ret_*` is the underlying's return from the fill; `pnl_*` signs it
by the action (buy +, sell/close -). Option actions are proxied by the
underlying (`instrument=option_proxy`). These are directional marks for "would
this rule have helped", not a P&L claim about the agent.
"""
from __future__ import annotations

import ast
import sqlite3
import statistics
from decimal import Decimal
from itertools import pairwise
from typing import Any

from allpath_trade.research.bars import BarCache, DailyBar
from allpath_trade.research.db import et_date
from allpath_trade.research.regime_metrics import window_for
from allpath_trade.research.revisions import load_revisions
from allpath_trade.strategy.conditions import ConditionError, evaluate_condition, parse_condition

POSITION_VARS = {"position_weight", "position_qty", "target_weight", "avg_entry_price",
                 "pnl_pct"}


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def relax_condition(text: str) -> tuple[str | None, bool]:
    """(price-only condition text, whether anything was dropped). "" means
    nothing price-related is left (always true); None means unevaluable."""
    try:
        body = parse_condition(text).body
    except ConditionError:
        return None, False
    if not (_names(body) & POSITION_VARS):
        return text, False

    def keep(node: ast.AST) -> list[ast.AST] | None:
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            out: list[ast.AST] = []
            for v in node.values:
                k = keep(v)
                if k is None:
                    return None
                out.extend(k)
            return out
        if not (_names(node) & POSITION_VARS):
            return [node]
        if isinstance(node, ast.Compare):
            return []
        return None  # OR / NOT mixing position terms: cannot relax soundly

    kept = keep(body)
    if kept is None:
        return None, True
    return " and ".join(ast.unparse(k) for k in kept), True


def _literals(text: str) -> list[float]:
    return [float(n.value) for n in ast.walk(parse_condition(text).body)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))]


def _holds(text: str, price: float) -> bool:
    return evaluate_condition(text, {"price": Decimal(str(price))})


def fires_on_bar(text: str, bar: DailyBar) -> str | None:
    """'close' / 'intraday' / None for a price-only condition on one bar."""
    if text == "":
        return "close"
    if _holds(text, bar.close):
        return "close"
    eps = max(1e-6, abs(bar.high) * 1e-7)
    points = {bar.low, bar.high, bar.open}
    for c in _literals(text):
        if bar.low <= c <= bar.high:
            points.update({c, min(bar.high, c + eps), max(bar.low, c - eps)})
    ordered = sorted(points)
    points.update((a + b) / 2 for a, b in pairwise(ordered))
    return "intraday" if any(_holds(text, p) for p in points) else None


def _side(action: str) -> tuple[str, str]:
    verb = (action or "").split(" ", 1)[0]
    return {
        "buy": ("buy", "stock"), "sell": ("sell", "stock"),
        "close_options": ("sell", "option_proxy"), "buy_call": ("buy", "option_proxy"),
        "buy_put": ("sell", "option_proxy"),
    }.get(verb, ("", "other"))


COLUMNS = [
    "review_id", "account", "source", "strategy", "ticker", "rule_id", "change",
    "variant", "decision", "decision_ts", "decision_date", "window", "condition",
    "replayed_condition", "position_terms_relaxed", "action", "side", "instrument",
    "fired_on", "fire_basis", "n_fire_days", "fill_px", "px_5d", "px_20d", "px_today",
    "ret_5d", "ret_20d", "ret_today", "pnl_5d", "pnl_20d", "pnl_today", "asof", "note",
]


def _targets(revisions, sources):
    for r in revisions:
        if sources is not None and r.source not in sources:
            continue
        if not r.resolved_ts:
            continue
        if r.status == "rejected":
            for rid in r.diff.rules_added + r.diff.rules_modified:
                change = "added" if rid in r.diff.rules_added else "modified"
                yield r, rid, change, "proposed", "vetoed", r.diff.new_rules[rid]
            for rid in r.diff.rules_modified + r.diff.rules_removed:
                change = "modified" if rid in r.diff.rules_modified else "removed"
                yield r, rid, change, "kept", "vetoed", r.diff.old_rules[rid]
        elif r.applied:
            for rid in r.diff.rules_removed:
                yield r, rid, "removed", "removed", "removed", r.diff.old_rules[rid]


def replay_rows(conn: sqlite3.Connection, cache: BarCache, *, asof: str,
                sources: tuple[str, ...] | None = None,
                start: str = "0000-00-00", end: str = "9999-99-99") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r, rid, change, variant, decision, spec in _targets(load_revisions(conn), sources):
        decision_date = et_date(r.resolved_ts) or ""
        if not (start <= decision_date <= end) or decision_date > asof:
            continue
        condition = str(spec.get("condition", ""))
        action = str(spec.get("action", ""))
        side, instrument = _side(action)
        replayed, relaxed = relax_condition(condition)
        row: dict[str, Any] = {c: "" for c in COLUMNS}
        row.update({
            "review_id": r.review_id, "account": r.account, "source": r.source,
            "strategy": r.strategy_id, "ticker": r.ticker, "rule_id": rid,
            "change": change, "variant": variant, "decision": decision,
            "decision_ts": r.resolved_ts, "decision_date": decision_date,
            "window": window_for(et_date(r.proposed_ts) or ""), "condition": condition,
            "replayed_condition": "" if replayed is None else replayed,
            "position_terms_relaxed": int(relaxed), "action": action, "side": side,
            "instrument": instrument, "asof": asof,
        })
        rows.append(row)
        if replayed is None:
            row["note"] = "unevaluable condition"
            continue
        try:
            bars = [b for b in cache.get(r.ticker, decision_date, asof)
                    if b.date > decision_date]
        except LookupError as exc:
            row["note"] = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 -- a bad ticker must not stop the run
            row["note"] = f"bars unavailable: {exc}"
            continue
        if not bars:
            row["note"] = "no bars after decision"
            continue
        hits = [(i, basis) for i, b in enumerate(bars)
                if (basis := fires_on_bar(replayed, b)) is not None]
        row["n_fire_days"] = len(hits)
        if not hits:
            continue
        i, basis = hits[0]
        fill = bars[i].close
        row.update({"fired_on": bars[i].date, "fire_basis": basis, "fill_px": fill})
        sign = 1 if side == "buy" else -1 if side == "sell" else 0
        for label, j in (("5d", i + 5), ("20d", i + 20), ("today", len(bars) - 1)):
            if j < len(bars) and fill:
                px = bars[j].close
                ret = round(px / fill - 1, 6)
                row[f"px_{label}"] = px
                row[f"ret_{label}"] = ret
                row[f"pnl_{label}"] = round(sign * ret, 6) if sign else ""
    return rows


def summary_markdown(rows: list[dict[str, Any]], *, db_label: str, asof: str) -> str:
    lines = [
        "# Counterfactual rule replay summary",
        "",
        (f"Source: `{db_label}` (read-only), daily bars to {asof}. pnl = underlying "
         "return from the fire-day close, signed by the action; directional only."),
        "",
        ("| decision | variant | change | rows | fired | of which intraday-only | "
         "relaxed | mean pnl +5d | mean pnl +20d | mean pnl to asof | pnl>0 to asof |"),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["decision"], r["variant"], r["change"]), []).append(r)

    def mean(rs, k):
        xs = [r[k] for r in rs if r[k] != ""]
        return f"{statistics.mean(xs):+.3f} (n={len(xs)})" if xs else ""

    for (d, v, c), rs in sorted(groups.items()):
        fired = [r for r in rs if r["fired_on"]]
        pos = sum(1 for r in fired if r["pnl_today"] != "" and r["pnl_today"] > 0)
        lines.append(
            f"| {d} | {v} | {c} | {len(rs)} | {len(fired)} | "
            f"{sum(1 for r in fired if r['fire_basis'] == 'intraday')} | "
            f"{sum(r['position_terms_relaxed'] for r in rs)} | {mean(fired, 'pnl_5d')} | "
            f"{mean(fired, 'pnl_20d')} | {mean(fired, 'pnl_today')} | "
            f"{pos}/{len(fired)} |")
    lines += ["", ("See the module docstring of `allpath_trade/research/replay.py` for "
                   "the method and its limits."), ""]
    return "\n".join(lines)
