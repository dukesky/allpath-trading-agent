"""TDSC R3 (dev-requests item 4): rule outcomes.

One row per (revision, rule, change) for every rule a revision touched
(reflection revisions by default). Each row has an outcome window:

- `live_after`: an applied add/modify. From the apply time until the rule's
  next change by a later applied revision (any source), or the data end.
  Did the rule as written fire, how often, the first fire and its trade.
- `before_removal`: an applied removal. From when the rule last took its
  final spec (the latest earlier applied add/modify, else the start of the
  data) to the removal. Did it fire before it was removed?
- `kept_after_veto`: a vetoed modify/remove. The original rule stayed; from
  the veto until its next applied change. Did the kept rule fire?
- `none`: a vetoed add (the rule never existed; see R2's counterfactual
  replay) or a still-pending proposal.

Fires are the union of three records, deduplicated per rule:
`sentinel` observations (one per fire since 08-11), `rule_fires` rows (since
09-28) and trades whose reason names the rule (catches an auto fill with no
fire record). A rule_fires row or a trade within FIRE_MERGE_SECONDS after a
known fire of the same rule is the same fire.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from allpath_trade.research.db import parse_ts
from allpath_trade.research.regime_metrics import _rule_from_reason, window_for
from allpath_trade.research.revisions import Revision, load_revisions

FIRE_MERGE_SECONDS = 30 * 60
TRADE_LINK_SECONDS = 5 * 24 * 3600
# The sentinel observation is written AFTER dispatch, so an auto trade's ts
# can precede its fire record by a few seconds.
FIRE_LEAD_SECONDS = 120
_OBS_RE = re.compile(r"^(?P<sid>[^/\s]+)/(?P<rid>\S+) .*?: (?P<disp>[a-z_]+)\b")


@dataclass
class Fire:
    ts: str
    account: str
    strategy_id: str
    rule_id: str
    disposition: str
    source: str  # observation | rule_fires | trade


def _secs(a: str, b: str) -> float:
    return (parse_ts(b) - parse_ts(a)).total_seconds()


def load_fires(conn: sqlite3.Connection) -> list[Fire]:
    raw: list[Fire] = []
    for r in conn.execute("SELECT ts, account, text FROM observations"
                          " WHERE source = 'sentinel'"):
        if m := _OBS_RE.match(r["text"]):
            raw.append(Fire(r["ts"], r["account"], m["sid"], m["rid"], m["disp"],
                            "observation"))
    for r in conn.execute("SELECT ts, account, strategy_id, rule_id FROM rule_fires"):
        raw.append(Fire(r["ts"], r["account"], r["strategy_id"], r["rule_id"], "",
                        "rule_fires"))
    for r in conn.execute("SELECT ts, account, strategy_id, reason, status FROM trades"
                          " WHERE strategy_id IS NOT NULL"):
        rid = _rule_from_reason(r["reason"] or "")
        if rid:
            raw.append(Fire(r["ts"], r["account"], r["strategy_id"], rid,
                            f"trade_{r['status']}", "trade"))
    # Merge per rule: observations first (they carry the disposition), then
    # rule_fires, then trades; a later-ranked record close after a kept fire
    # is the same fire.
    rank = {"observation": 0, "rule_fires": 1, "trade": 2}
    by_rule: dict[tuple[str, str, str], list[Fire]] = {}
    for f in sorted(raw, key=lambda f: (rank[f.source], f.ts)):
        kept = by_rule.setdefault((f.account, f.strategy_id, f.rule_id), [])
        window = 120 if f.source == "rule_fires" else FIRE_MERGE_SECONDS
        if any(-FIRE_LEAD_SECONDS <= _secs(k.ts, f.ts) <= window for k in kept):
            continue
        kept.append(f)
    return sorted((f for fs in by_rule.values() for f in fs), key=lambda f: f.ts)


@dataclass
class _Trade:
    id: int
    ts: str
    status: str


def _trades_by_rule(conn: sqlite3.Connection) -> dict[tuple[str, str, str], list[_Trade]]:
    out: dict[tuple[str, str, str], list[_Trade]] = {}
    for r in conn.execute("SELECT id, ts, account, strategy_id, reason, status FROM trades"
                          " WHERE strategy_id IS NOT NULL ORDER BY ts"):
        rid = _rule_from_reason(r["reason"] or "")
        if rid:
            out.setdefault((r["account"], r["strategy_id"], rid), []).append(
                _Trade(r["id"], r["ts"], r["status"]))
    return out


def _data_end(conn: sqlite3.Connection) -> str:
    ends = [conn.execute(f"SELECT MAX(ts) FROM {t}").fetchone()[0]  # fixed table names
            for t in ("observations", "trades", "rule_fires", "pending_reviews")]
    return max(e for e in ends if e)


def _applied_ts(r: Revision) -> str:
    return r.resolved_ts or r.proposed_ts


def _changes(r: Revision) -> list[tuple[str, str]]:
    return ([(rid, "added") for rid in r.diff.rules_added]
            + [(rid, "modified") for rid in r.diff.rules_modified]
            + [(rid, "removed") for rid in r.diff.rules_removed])


COLUMNS = [
    "review_id", "account", "source", "strategy_id", "rule_id", "change", "status",
    "applied", "proposed_ts", "decided_ts", "window", "condition", "action",
    "window_kind", "window_start", "window_end", "fired", "n_fires", "first_fire_ts",
    "first_fire_disposition", "last_fire_ts", "trade_id", "trade_status",
    "later_removed_by", "later_removed_by_source",
]


def rule_outcome_rows(conn: sqlite3.Connection, *,
                      sources: tuple[str, ...] | None = ("reflection",),
                      start: str = "0000-00-00", end: str = "9999-99-99") -> list[dict[str, Any]]:
    revisions = load_revisions(conn)
    fires = load_fires(conn)
    trades = _trades_by_rule(conn)
    data_end = _data_end(conn)
    fires_by_rule: dict[tuple[str, str, str], list[Fire]] = {}
    for f in fires:
        fires_by_rule.setdefault((f.account, f.strategy_id, f.rule_id), []).append(f)
    # Applied changes per rule, in time order: (ts, review, change).
    applied: dict[tuple[str, str, str], list[tuple[str, Revision, str]]] = {}
    for r in revisions:
        if r.applied:
            for rid, change in _changes(r):
                applied.setdefault((r.account, r.strategy_id, rid), []).append(
                    (_applied_ts(r), r, change))
    for v in applied.values():
        v.sort(key=lambda x: (x[0], x[1].review_id))

    rows: list[dict[str, Any]] = []
    for r in revisions:
        if sources is not None and r.source not in sources:
            continue
        if not (start <= r.proposed_date_et <= end):
            continue
        decided = r.resolved_ts or ""
        for rid, change in _changes(r):
            key = (r.account, r.strategy_id, rid)
            history = applied.get(key, [])
            spec = (r.diff.old_rules if change == "removed" else r.diff.new_rules).get(rid, {})
            kind, w_start, w_end = "none", "", ""
            if r.applied and change in ("added", "modified"):
                kind, w_start = "live_after", _applied_ts(r)
                w_end = next((ts for ts, o, _ in history
                              if ts > w_start and o.review_id != r.review_id), data_end)
            elif r.applied and change == "removed":
                kind, w_end = "before_removal", _applied_ts(r)
                w_start = max((ts for ts, o, c in history if ts < w_end
                               and c in ("added", "modified")), default="")
            elif r.status == "rejected" and change in ("modified", "removed") and decided:
                kind, w_start = "kept_after_veto", decided
                w_end = next((ts for ts, _, _ in history if ts > w_start), data_end)
            in_window = [f for f in fires_by_rule.get(key, [])
                         if kind != "none" and (not w_start or f.ts >= w_start)
                         and f.ts < w_end]
            first = in_window[0] if in_window else None
            trade = None
            if first is not None:
                # The trade that followed THIS fire: before the rule's next
                # fire, within TRADE_LINK_SECONDS (an approval can take days).
                all_fires = fires_by_rule.get(key, [])
                nxt = next((f.ts for f in all_fires if f.ts > first.ts), None)
                trade = next((t for t in trades.get(key, [])
                              if -FIRE_LEAD_SECONDS <= _secs(first.ts, t.ts)
                              <= TRADE_LINK_SECONDS
                              and (nxt is None or t.ts < nxt)), None)
            removed_by = None
            if change == "added" and r.applied:
                removed_by = next((o for ts, o, c in history
                                   if c == "removed" and ts > _applied_ts(r)), None)
            rows.append({
                "review_id": r.review_id, "account": r.account, "source": r.source,
                "strategy_id": r.strategy_id, "rule_id": rid, "change": change,
                "status": r.status, "applied": int(r.applied),
                "proposed_ts": r.proposed_ts, "decided_ts": decided,
                "window": window_for(r.proposed_date_et),
                "condition": spec.get("condition", ""), "action": spec.get("action", ""),
                "window_kind": kind, "window_start": w_start, "window_end": w_end,
                "fired": int(bool(in_window)) if kind != "none" else "",
                "n_fires": len(in_window) if kind != "none" else "",
                "first_fire_ts": first.ts if first else "",
                "first_fire_disposition": first.disposition if first else "",
                "last_fire_ts": in_window[-1].ts if in_window else "",
                "trade_id": trade.id if trade else "",
                "trade_status": trade.status if trade else "",
                "later_removed_by": removed_by.review_id if removed_by else "",
                "later_removed_by_source": removed_by.source if removed_by else "",
            })
    return rows


def summary_markdown(rows: list[dict[str, Any]], *, db_label: str, sources: str) -> str:
    lines = [
        "# Rule outcomes summary",
        "",
        (f"Source: `{db_label}` (read-only); revisions from: {sources}. "
         "One row in rule_outcomes.csv per (revision, rule, change)."),
        "",
        "| window | account | change | window kind | rows | fired | fired % | later removed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["window"] or "-", r["account"], r["change"], r["window_kind"]),
                          []).append(r)
    for (w, a, c, k), rs in sorted(groups.items()):
        fired = sum(1 for r in rs if r["fired"] == 1)
        scored = sum(1 for r in rs if r["fired"] != "")
        pct = f"{100 * fired / scored:.0f}%" if scored else ""
        removed = sum(1 for r in rs if r["later_removed_by"] != "")
        lines.append(f"| {w} | {a} | {c} | {k} | {len(rs)} | {fired} | {pct} | {removed} |")
    lines += [
        "",
        ("Fires merge sentinel observations (from 08-11), rule_fires (from 09-28) and "
         "trades naming the rule. `fired` is about the rule as written in that "
         "window; daily-bar counterfactuals for vetoed adds and removed rules are R2."),
        "",
    ]
    return "\n".join(lines)
