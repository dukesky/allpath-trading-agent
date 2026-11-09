"""TDSC R4: regime metrics export, one row per account per ET day.

Read-only: the caller passes a connection from `research.db.open_readonly`.
Every day is the US/Eastern calendar date of the stored UTC timestamp (the
convention `reports.date` already uses). Rates in the summary use
reflection-days (days with an `ok` report) as the denominator, never calendar
days (TDSC plan §2.4).

Known limits (also printed into summary.md):
- `llm_usage` has no account column: token and model columns are system-wide
  except `reflection_tokens_*`, which read the `reflection:<account>` purpose
  that R5 adds (zero before it is deployed).
- Breaker resets leave no DB trace (the CLI only clears app_state), so the
  known resets are listed in KNOWN_BREAKER_RESETS from the regime log.
- Order "auto" means a trade with no linked review (an auto+hard rule fired).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import yaml

from allpath_trade.research.db import ET, et_date, minutes_between, table_exists
from allpath_trade.research.revisions import Revision, load_revisions, regime_skips

ACCOUNTS = ("paper", "shadow")

# Regime windows by ET date (inclusive), from allpath-paper/data/regime-log.md
# and TDSC plan §2. B started the evening of 08-27, after that day's
# human-gated reflection, so 08-27 belongs to A. Days outside every window
# (10-03..10-24: interim human run, accidental auto run) get "".
WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("A", "2026-08-11", "2026-08-27"),
    ("B", "2026-08-28", "2026-10-02"),
    ("A'", "2026-10-25", "2026-11-30"),
    ("B'", "2026-12-01", "2026-12-31"),
)

# Breaker resets are not recorded in the DB. Source: regime-log.md
# ("2026-11-06 22:28:41 both drawdown breakers reset"). Append future resets
# here (and to the regime log) when they happen.
KNOWN_BREAKER_RESETS: dict[str, tuple[str, ...]] = {
    "paper": ("2026-11-06T22:28:41+00:00",),
    "shadow": ("2026-11-06T22:28:41+00:00",),
}

PROPOSAL_TYPES = (
    ("reflection", "strategy_revision", "prop_reflection_revision"),
    ("chat", "strategy_revision", "prop_chat_revision"),
    ("sentinel", "order", "prop_sentinel_order"),
    ("chat", "order", "prop_chat_order"),
    ("sentinel", "option_order", "prop_sentinel_option_order"),
    ("chat", "shadow_edit", "prop_chat_shadow_edit"),
)

DAILY_COLUMNS = [
    "date", "account", "weekday", "window", "regime_db",
    "report_status", "report_model", "report_tokens",
    *[c for _, _, c in PROPOSAL_TYPES], "prop_other",
    "refl_rev_auto_applied", "refl_rev_human_approved", "refl_rev_vetoed",
    "refl_rev_superseded", "refl_rev_pending",
    "refl_rev_latency_min_median", "refl_rev_latency_min_max",
    "refl_rev_gate_rejections", "refl_rev_regime_skips",
    "refl_rev_held", "refl_rev_held_pending", "refl_rev_held_approved",
    "refl_rev_held_vetoed",
    "chat_rev_approved", "chat_rev_rejected", "chat_rev_superseded", "chat_rev_pending",
    "rev_applied_later_reversed", "rev_rules_added_then_removed", "rev_fields_reverted",
    "order_reviews_proposed", "order_reviews_approved", "order_reviews_rejected",
    "order_reviews_pending", "order_reviews_latency_min_median",
    "orders_auto_executed", "trades_filled", "trades_rejected", "trades_error",
    "breaker_trips", "breaker_state_eod",
    "strat_active_auto", "strat_active_confirm", "strat_active_notify",
    "models_chat", "models_review", "models_memory",
    "reflection_tokens_in", "reflection_tokens_out",
    "order_thread_msgs",
]


def window_for(day: str) -> str:
    for name, start, end in WINDOWS:
        if start <= day <= end:
            return name
    return ""


def _eod_utc(day: str) -> str:
    """The instant the ET day ends, as a UTC ISO string comparable to stored ts."""
    d = date.fromisoformat(day) + timedelta(days=1)
    return datetime.combine(d, time(0), tzinfo=ET).astimezone(UTC).isoformat()


def _days(start: str, end: str) -> list[str]:
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _median(xs: list[float]) -> float | str:
    return round(statistics.median(xs), 2) if xs else ""


@dataclass
class Order:
    trade_id: int | None
    review_id: int | None
    account: str
    ts: str
    ticker: str
    side: str
    status: str
    strategy_id: str
    rule_id: str
    path: str  # auto | review_approved | review_rejected | review_pending | gate_rejected | error

    @property
    def date_et(self) -> str:
        return et_date(self.ts) or ""


def _rule_from_reason(reason: str) -> str:
    # "strategy <sid> rule <rid>: <condition> -> <action>"
    if reason.startswith("strategy ") and " rule " in reason:
        tail = reason.split(" rule ", 1)[1]
        return tail.split(":", 1)[0].strip()
    return ""


def load_orders(conn: sqlite3.Connection) -> tuple[list[Order], list[sqlite3.Row]]:
    """Every trade row tagged with how it got there, plus order reviews that
    never produced a trade. Link: review `execution_result.order.id` ==
    `trades.broker_order_id`."""
    reviews = list(conn.execute(
        "SELECT id, account, ts, resolved_ts, status, strategy_id, rule_id, ticker,"
        " kind, source, execution_result FROM pending_reviews"
        " WHERE kind IN ('order', 'option_order')"))
    by_order_id: dict[tuple[str, str], sqlite3.Row] = {}
    for r in reviews:
        try:
            er = json.loads(r["execution_result"] or "{}")
        except json.JSONDecodeError:
            er = {}
        oid = (er.get("order") or {}).get("id") if isinstance(er, dict) else None
        if oid:
            by_order_id[(r["account"], str(oid))] = r
    orders: list[Order] = []
    linked: set[int] = set()
    for t in conn.execute("SELECT id, account, ts, ticker, side, status, strategy_id,"
                          " reason, broker_order_id FROM trades ORDER BY id"):
        rv = by_order_id.get((t["account"], str(t["broker_order_id"])))
        if rv is not None:
            linked.add(rv["id"])
            path = "review_approved"
        elif t["status"] == "rejected":
            path = "gate_rejected"
        elif t["status"] == "error":
            path = "error"
        else:
            path = "auto"
        orders.append(Order(
            trade_id=t["id"], review_id=rv["id"] if rv is not None else None,
            account=t["account"], ts=t["ts"], ticker=t["ticker"], side=t["side"],
            status=t["status"], strategy_id=t["strategy_id"] or "",
            rule_id=_rule_from_reason(t["reason"] or ""), path=path))
    for r in reviews:
        if r["id"] in linked or r["status"] == "approved":
            continue
        orders.append(Order(
            trade_id=None, review_id=r["id"], account=r["account"], ts=r["ts"],
            ticker=r["ticker"], side="", status=r["status"],
            strategy_id=r["strategy_id"] or "", rule_id=r["rule_id"] or "",
            path=f"review_{r['status']}"))
    return orders, reviews


def _strategy_history(conn: sqlite3.Connection) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """(account, strategy_id) -> [(ts, status, authorization)] from strategy_versions."""
    out: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for r in conn.execute("SELECT account, strategy_id, ts, content FROM strategy_versions"
                          " ORDER BY ts, id"):
        try:
            doc = yaml.safe_load(r["content"]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        out.setdefault((r["account"], r["strategy_id"]), []).append(
            (r["ts"], str(doc.get("status", "")), str(doc.get("authorization", ""))))
    return out


def _auth_counts(history, account: str, eod: str) -> dict[str, int]:
    counts = {"auto": 0, "confirm": 0, "notify": 0}
    for (acct, _sid), versions in history.items():
        if acct != account:
            continue
        current = None
        for ts, status, auth in versions:
            if ts < eod:
                current = (status, auth)
        if current and current[0] == "active" and current[1] in counts:
            counts[current[1]] += 1
    return counts


def _breaker_state(trips: list[str], resets: tuple[str, ...], eod: str) -> str:
    events = sorted([(t, "trip") for t in trips if t < eod]
                    + [(r, "reset") for r in resets if r < eod])
    return "tripped" if events and events[-1][1] == "trip" else "ok"


def daily_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    revisions = load_revisions(conn)
    orders, order_reviews = load_orders(conn)
    skips = regime_skips(conn)
    reports = {(r["account"], r["date"]): r for r in conn.execute(
        "SELECT account, date, status, model, tokens_used FROM reports")}
    proposals = list(conn.execute("SELECT account, ts, source, kind FROM pending_reviews"))
    history = _strategy_history(conn)
    trips: dict[str, list[str]] = {}
    for r in conn.execute("SELECT account, ts FROM observations WHERE source = 'breaker'"
                          " AND text LIKE 'drawdown breaker tripped%'"):
        trips.setdefault(r["account"], []).append(r["ts"])
    regimes = list(conn.execute("SELECT ts, account, regime FROM regime_events ORDER BY ts"))
    usage = list(conn.execute(
        "SELECT ts, tier, model, input_tokens, output_tokens, purpose FROM llm_usage"))
    thread_msgs = _order_thread_messages(conn)

    # Model per tier: every model called that day (successes and failures);
    # on a day with no call, the last model seen before it (still configured).
    calls = sorted([(u["ts"], u["tier"], u["model"]) for u in usage]
                   + ([(f["ts"], f["tier"], f["model"]) for f in conn.execute(
                       "SELECT ts, tier, model FROM llm_failures")]
                      if table_exists(conn, "llm_failures") else []))
    last_model: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for day in _days(start, end):
        eod = _eod_utc(day)
        weekday = date.fromisoformat(day).weekday() < 5
        day_usage = [u for u in usage if et_date(u["ts"]) == day]
        seen: dict[str, set[str]] = {}
        for ts, tier, model in calls:
            if ts >= eod:
                break
            if et_date(ts) == day:
                seen.setdefault(tier, set()).add(model)
            last_model[tier] = model
        models = {tier: "|".join(sorted(seen[tier])) if tier in seen
                  else last_model.get(tier, "") for tier in ("chat", "review", "memory")}
        for account in ACCOUNTS:
            row = _account_day(
                day, account, eod, revisions=revisions, orders=orders,
                order_reviews=order_reviews, skips=skips, reports=reports,
                proposals=proposals, history=history, trips=trips.get(account, []),
                regimes=regimes, day_usage=day_usage)
            row["weekday"] = int(weekday)
            row["order_thread_msgs"] = sum(1 for m in thread_msgs if m["account"] == account
                                           and et_date(m["ts"]) == day)
            row.update({f"models_{t}": m for t, m in models.items()})
            active = (row["report_status"] != "none" or row["breaker_trips"]
                      or any(row[c] for _, _, c in PROPOSAL_TYPES) or row["prop_other"]
                      or row["trades_filled"] or row["trades_rejected"])
            if weekday or active:
                rows.append({c: row.get(c, "") for c in DAILY_COLUMNS})
    return rows


def _order_thread_messages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Human messages on Telegram order threads (TELEGRAM_ORDER_THREADS);
    a DB from before that feature has no table and so no messages."""
    if not table_exists(conn, "order_thread_messages"):
        return []
    return list(conn.execute("SELECT * FROM order_thread_messages ORDER BY ts, id"))


ORDER_THREAD_COLUMNS = [
    "id", "account", "ts", "date_et", "window", "review_id", "card_message_id",
    "telegram_message_id", "user_id", "forwarded", "text",
]


def order_thread_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    """One row per human message on an order thread (who, when, which
    card/review, the text as received). The text is the owner's own words
    -- keep this export out of the public trading repo."""
    rows = []
    for m in _order_thread_messages(conn):
        day = et_date(m["ts"])
        if day is None or not (start <= day <= end):
            continue
        rows.append({"id": m["id"], "account": m["account"], "ts": m["ts"],
                     "date_et": day, "window": window_for(day),
                     "review_id": m["review_id"], "card_message_id": m["card_message_id"],
                     "telegram_message_id": m["telegram_message_id"],
                     "user_id": m["user_id"], "forwarded": m["forwarded"],
                     "text": m["text"]})
    return rows


def _account_day(day: str, account: str, eod: str, *, revisions: list[Revision],
                 orders: list[Order], order_reviews, skips, reports, proposals,
                 history, trips, regimes, day_usage) -> dict[str, Any]:
    row: dict[str, Any] = {"date": day, "account": account, "window": window_for(day)}
    rep = reports.get((account, day))
    row["report_status"] = rep["status"] if rep else "none"
    row["report_model"] = rep["model"] if rep else ""
    row["report_tokens"] = rep["tokens_used"] if rep else 0
    regime = ""
    for ev in regimes:
        if ev["account"] == account and ev["ts"] < eod:
            regime = ev["regime"]
    row["regime_db"] = regime

    known = {(s, k): c for s, k, c in PROPOSAL_TYPES}
    for _, _, col in PROPOSAL_TYPES:
        row[col] = 0
    row["prop_other"] = 0
    for p in proposals:
        if p["account"] == account and et_date(p["ts"]) == day:
            row[known.get((p["source"], p["kind"]), "prop_other")] += 1

    todays = [r for r in revisions if r.account == account and r.proposed_date_et == day]
    refl = [r for r in todays if r.source == "reflection"]
    chat = [r for r in todays if r.source != "reflection"]
    row["refl_rev_auto_applied"] = sum(r.resolver == "auto" for r in refl)
    row["refl_rev_human_approved"] = sum(r.resolver == "human" and r.status == "approved"
                                         for r in refl)
    row["refl_rev_vetoed"] = sum(r.status == "rejected" for r in refl)
    row["refl_rev_superseded"] = sum(r.status == "superseded" for r in refl)
    row["refl_rev_pending"] = sum(r.status == "pending" for r in refl)
    lat = [r.latency_min for r in refl if r.latency_min is not None]
    row["refl_rev_latency_min_median"] = _median(lat)
    row["refl_rev_latency_min_max"] = max(lat) if lat else ""
    row["refl_rev_gate_rejections"] = sum(r.gate_rejected for r in refl)
    held = [r for r in refl if r.held]
    row["refl_rev_held"] = len(held)
    row["refl_rev_held_pending"] = sum(r.status == "pending" for r in held)
    row["refl_rev_held_approved"] = sum(r.status == "approved" for r in held)
    row["refl_rev_held_vetoed"] = sum(r.status == "rejected" for r in held)
    row["refl_rev_regime_skips"] = sum(1 for a, ts in skips
                                       if a == account and et_date(ts) == day)
    for status, col in (("approved", "chat_rev_approved"), ("rejected", "chat_rev_rejected"),
                        ("superseded", "chat_rev_superseded"), ("pending", "chat_rev_pending")):
        row[col] = sum(r.status == status for r in chat)
    applied = [r for r in todays if r.applied]
    row["rev_applied_later_reversed"] = sum(r.reversed_by is not None for r in applied)
    row["rev_rules_added_then_removed"] = sum(len(r.rules_added_then_removed) for r in applied)
    row["rev_fields_reverted"] = sum(len(r.fields_reverted) for r in applied)

    ors = [r for r in order_reviews if r["account"] == account and et_date(r["ts"]) == day]
    row["order_reviews_proposed"] = len(ors)
    for status in ("approved", "rejected", "pending"):
        row[f"order_reviews_{status}"] = sum(r["status"] == status for r in ors)
    row["order_reviews_latency_min_median"] = _median(
        [m for r in ors if (m := minutes_between(r["ts"], r["resolved_ts"])) is not None])
    day_trades = [o for o in orders if o.trade_id is not None
                  and o.account == account and o.date_et == day]
    row["orders_auto_executed"] = sum(o.path == "auto" for o in day_trades)
    row["trades_filled"] = sum(o.status == "filled" for o in day_trades)
    row["trades_rejected"] = sum(o.status == "rejected" for o in day_trades)
    row["trades_error"] = sum(o.status == "error" for o in day_trades)

    row["breaker_trips"] = sum(1 for t in trips if et_date(t) == day)
    row["breaker_state_eod"] = _breaker_state(trips, KNOWN_BREAKER_RESETS.get(account, ()), eod)
    counts = _auth_counts(history, account, eod)
    row["strat_active_auto"] = counts["auto"]
    row["strat_active_confirm"] = counts["confirm"]
    row["strat_active_notify"] = counts["notify"]
    purpose = f"reflection:{account}"
    row["reflection_tokens_in"] = sum(u["input_tokens"] for u in day_usage
                                      if u["purpose"] == purpose)
    row["reflection_tokens_out"] = sum(u["output_tokens"] for u in day_usage
                                       if u["purpose"] == purpose)
    return row


REVISION_COLUMNS = [
    "review_id", "account", "source", "strategy_id", "ticker", "proposed_ts",
    "proposed_date_et", "window", "status", "resolver", "resolved_ts", "latency_min",
    "gate_rejected", "held", "hold_reasons", "hold_categories", "is_new", "rules_added", "rules_removed", "rules_modified",
    "fields_changed", "reversed_by", "rules_added_then_removed", "fields_reverted",
]


def revision_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    out = []
    for r in load_revisions(conn):
        d = r.proposed_date_et
        if not (start <= d <= end):
            continue
        out.append({
            "review_id": r.review_id, "account": r.account, "source": r.source,
            "strategy_id": r.strategy_id, "ticker": r.ticker, "proposed_ts": r.proposed_ts,
            "proposed_date_et": d, "window": window_for(d), "status": r.status,
            "resolver": r.resolver, "resolved_ts": r.resolved_ts or "",
            "latency_min": "" if r.latency_min is None else r.latency_min,
            "gate_rejected": int(r.gate_rejected), "held": int(r.held),
            "hold_reasons": "; ".join(r.hold_reasons),
            "hold_categories": ";".join(r.hold_categories), "is_new": int(r.is_new),
            "rules_added": ";".join(r.diff.rules_added),
            "rules_removed": ";".join(r.diff.rules_removed),
            "rules_modified": ";".join(r.diff.rules_modified),
            "fields_changed": ";".join(r.diff.fields_changed),
            "reversed_by": r.reversed_by or "",
            "rules_added_then_removed": ";".join(r.rules_added_then_removed),
            "fields_reverted": ";".join(r.fields_reverted),
        })
    return out


HOLD_COLUMNS = ["review_id", "account", "source", "strategy_id", "proposed_date_et",
                "window", "status", "reason", "category"]


def hold_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    """One row per (held revision, reason): the F1 allow-list's reasons for
    holding a revision for a human under auto."""
    out = []
    for r in load_revisions(conn):
        d = r.proposed_date_et
        if not r.held or not (start <= d <= end):
            continue
        for reason, category in zip(r.hold_reasons, r.hold_categories, strict=True):
            out.append({"review_id": r.review_id, "account": r.account,
                        "source": r.source, "strategy_id": r.strategy_id,
                        "proposed_date_et": d, "window": window_for(d),
                        "status": r.status, "reason": reason, "category": category})
    return out


ORDER_COLUMNS = ["trade_id", "review_id", "account", "ts", "date_et", "window", "ticker",
                 "side", "status", "strategy_id", "rule_id", "path"]


def order_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    orders, _ = load_orders(conn)
    out = []
    for o in sorted(orders, key=lambda o: o.ts):
        if not (start <= o.date_et <= end):
            continue
        out.append({"trade_id": o.trade_id or "", "review_id": o.review_id or "",
                    "account": o.account, "ts": o.ts, "date_et": o.date_et,
                    "window": window_for(o.date_et), "ticker": o.ticker, "side": o.side,
                    "status": o.status, "strategy_id": o.strategy_id,
                    "rule_id": o.rule_id, "path": o.path})
    return out


LLM_COLUMNS = ["date", "tier", "model", "purpose", "calls", "input_tokens", "output_tokens"]
LLM_FAILURE_COLUMNS = ["date", "tier", "model", "error_class", "failures"]


def llm_rows(conn: sqlite3.Connection, start: str, end: str) -> tuple[list[dict], list[dict]]:
    agg: dict[tuple, list[int]] = {}
    for u in conn.execute("SELECT ts, tier, model, purpose, input_tokens, output_tokens"
                          " FROM llm_usage"):
        d = et_date(u["ts"])
        if not (start <= d <= end):
            continue
        a = agg.setdefault((d, u["tier"], u["model"], u["purpose"]), [0, 0, 0])
        a[0] += 1
        a[1] += u["input_tokens"]
        a[2] += u["output_tokens"]
    usage = [dict(zip(LLM_COLUMNS, (*k, *v), strict=True)) for k, v in sorted(agg.items())]
    fails: dict[tuple, int] = {}
    if table_exists(conn, "llm_failures"):
        for f in conn.execute("SELECT ts, tier, model, error_class FROM llm_failures"):
            d = et_date(f["ts"])
            if start <= d <= end:
                k = (d, f["tier"], f["model"], f["error_class"])
                fails[k] = fails.get(k, 0) + 1
    failures = [dict(zip(LLM_FAILURE_COLUMNS, (*k, v), strict=True))
                for k, v in sorted(fails.items())]
    return usage, failures


def summary_markdown(daily: list[dict[str, Any]], *, db_label: str, start: str,
                     end: str, holds: list[dict[str, Any]] | None = None) -> str:
    lines = [
        "# Regime metrics summary",
        "",
        (f"Source: `{db_label}` (opened read-only), ET dates {start}..{end}. "
         "Generated by `allpath-research regime-metrics`."),
        "",
        ("Rates are per reflection-day (days with an `ok` report). Revision counts "
         "are by proposal date. `auto` = an auto-apply observation exists for the "
         "row; `vetoed` = rejected by a human."),
        "",
        ("| window | account | refl-days | failed | refl props | auto | human ok | "
         "vetoed | superseded | pending | gate rej | held | reversed later | props/refl-day | "
         "median latency (min) | order reviews | ord ok/rej | auto fills | gate-rejected trades |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, _, _ in WINDOWS:
        for account in ACCOUNTS:
            rs = [r for r in daily if r["window"] == name and r["account"] == account]
            if not rs:
                continue
            refl_days = sum(r["report_status"] == "ok" for r in rs)
            failed = sum(r["report_status"] == "failed" for r in rs)
            props = sum(r["prop_reflection_revision"] for r in rs)
            lat = [r["refl_rev_latency_min_median"] for r in rs
                   if r["refl_rev_latency_min_median"] != ""]
            rate = f"{props / refl_days:.2f}" if refl_days else ""
            lines.append(
                f"| {name} | {account} | {refl_days} | {failed} | {props} | "
                f"{sum(r['refl_rev_auto_applied'] for r in rs)} | "
                f"{sum(r['refl_rev_human_approved'] for r in rs)} | "
                f"{sum(r['refl_rev_vetoed'] for r in rs)} | "
                f"{sum(r['refl_rev_superseded'] for r in rs)} | "
                f"{sum(r['refl_rev_pending'] for r in rs)} | "
                f"{sum(r['refl_rev_gate_rejections'] for r in rs)} | "
                f"{sum(r['refl_rev_held'] for r in rs)} | "
                f"{sum(r['rev_applied_later_reversed'] for r in rs)} | {rate} | "
                f"{_median(lat)} | {sum(r['order_reviews_proposed'] for r in rs)} | "
                f"{sum(r['order_reviews_approved'] for r in rs)}/"
                f"{sum(r['order_reviews_rejected'] for r in rs)} | "
                f"{sum(r['orders_auto_executed'] for r in rs)} | "
                f"{sum(r['trades_rejected'] for r in rs)} |")
    lines += [
        "",
        "## Holds by reason",
        "",
        ("Under auto, a reflection revision outside the F1 auto-apply allow-list is "
         "held for a human (`held`). One revision can carry several reasons; `held` "
         "in the table above counts revisions, this table counts reasons."),
        "",
        "| window | account | category | reasons | revisions |",
        "|---|---|---|---|---|",
    ]
    by_cat: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for h in holds or []:
        by_cat.setdefault((h["window"] or "-", h["account"], h["category"]), []).append(h)
    for (w, a, c), hs in sorted(by_cat.items()):
        lines.append(f"| {w} | {a} | {c} | {len(hs)} | "
                     f"{len({h['review_id'] for h in hs})} |")
    if not by_cat:
        lines.append("| - | - | (no holds in range) | 0 | 0 |")
    lines += [
        "",
        ("`median latency` is the median of daily medians. `reversed later` counts "
         "applied revisions (any source) proposed in the window whose added rule was "
         "later removed or whose changed field/rule was later put back, by any later "
         "applied revision."),
        "",
        ("Caveats: llm_usage has no account column (token/model columns are "
         "system-wide; `reflection_tokens_*` only after R5). Breaker resets come from "
         "KNOWN_BREAKER_RESETS (regime log), not the DB. Within B, paper strategies "
         "ran at confirm 09-14..09-25 (see `strat_active_*`), so order metrics hold "
         "the regime definition only outside that span."),
        "",
    ]
    return "\n".join(lines)
