"""Per-revision facts shared by the regime export (R4), rule outcomes (R3) and
the counterfactual replay (R2).

Source of truth is `pending_reviews` rows of kind `strategy_revision`: each
snapshot carries the exact `old_yaml` the proposal was made against and the
proposed `new_yaml`, so the diff is known for approved AND vetoed proposals.

Who resolved a row:
- `auto`: an `observations` row with source `reflection_auto_apply` says
  "revision #N ... auto-applied" (the experiment-mode applier writes exactly
  one per auto-applied row; paper-1 metrics used the same discriminator).
- `human`: approved/rejected/superseded with no such observation.
- `none`: still pending.
`gate_rejected` marks a "revision #N ... NOT auto-applied (<guard error>)"
observation: the applier's guards refused an auto-apply and left it pending.

`held` (TDSC F1, live from main 8c7a5d7): under auto, a revision whose diff
leaves the auto-apply allow-list (`strategy/auto_gate.py`) is held for a
human, observed as "revision #N for X held for human: <r1>; <r2>. Not
auto-applied (auto-apply allow-list); left pending" and stored in
`pending_reviews.hold_reason` (the fallback when no observation matches). A
held row stays pending until a human decides it, so it can be held AND
human-decided (`resolver=human`), never `auto`. `hold_reasons` are the
allow-list's reason strings; `hold_categories` map each to a stable label
(`hold_category`) for counting.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import yaml

from allpath_trade.research.db import et_date, minutes_between

RULE_SPEC_KEYS = ("type", "condition", "action", "rearm")
_SKIP_FIELDS = {"rules", "thesis", "version", "name", "id"}
_AUTO_RE = re.compile(r"revision #(\d+) for \S+ auto-applied")
_GATE_RE = re.compile(r"revision #(\d+) for \S+ NOT auto-applied \(")
_HELD_RE = re.compile(r"revision #(\d+) for \S+ held for human: (.*)\. Not auto-applied "
                      r"\(auto-apply allow-list\)", re.DOTALL)
_REGIME_SKIP_RE = re.compile(r"NOT auto-applied: revision auto-apply was switched off")


# Ordered: the first match wins. Mirrors the reason strings auto_gate.py and
# Reflector._auto_apply_holds write; anything new lands in "other".
_HOLD_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx)) for name, rx in (
        ("new_strategy", r"^proposes a new strategy"),
        ("unparseable", (r"^(could not read|could not classify|proposal does not parse"
                         r"|no parseable base)")),
        ("retarget_ticker", r"^changes position\.ticker"),
        ("size_increase", r"^(raises|sets|removes) position\."),
        ("removes_exit", r"^removes sell rule "),
        ("adds_rule", r"^adds rule "),
        ("frozen_field", r"^changes (authorization|status)$"),
        ("notify_off", r"^turns off notify_email"),
        ("flips_side", r"^changes \S+ from (buy|sell) to (buy|sell)"),
        ("rule_type", r" type (hard|soft) -> (hard|soft)"),
        ("rearm", r"fire (more|less) often \(rearm\)"),
        ("loosens_exit", r"^(sells less on |loosens sell rule )"),
        ("loosens_entry", r"^loosens buy rule "),
        ("buys_more", r"^buys more on "),
        ("condition_rewrite", r"^(changes the condition structure of|moves an equality in) "),
        ("changes_action", r"^changes the action of "),
        ("disables_rule", r"^disables "),
        ("rearms_rule", r"^re-arms "),
        ("other_field", r"^changes \S+$"),
    ))


def hold_category(reason: str) -> str:
    for name, rx in _HOLD_CATEGORIES:
        if rx.search(reason):
            return name
    return "other"


def _split_reasons(text: str) -> list[str]:
    return [r.strip() for r in text.split("; ") if r.strip()]


def _load(text: str) -> dict:
    if not text or not text.strip():
        return {}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _rules(doc: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in doc.get("rules") or []:
        if isinstance(r, dict) and r.get("id") is not None:
            out[str(r["id"])] = r
    return out


def rule_spec(r: dict | None) -> tuple | None:
    if r is None:
        return None
    return tuple(None if r.get(k) is None else str(r.get(k)) for k in RULE_SPEC_KEYS)


def _flatten(doc: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in doc.items():
        if not prefix and k in _SKIP_FIELDS:
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{key}."))
        else:
            out[key] = "" if v is None else str(v)
    return out


@dataclass
class YamlDiff:
    rules_added: list[str]
    rules_removed: list[str]
    rules_modified: list[str]
    fields_changed: dict[str, tuple[str | None, str | None]]
    old_rules: dict[str, dict]
    new_rules: dict[str, dict]
    new_fields: dict[str, str]


def diff_yaml(old_yaml: str, new_yaml: str) -> YamlDiff:
    old, new = _load(old_yaml), _load(new_yaml)
    o_rules, n_rules = _rules(old), _rules(new)
    o_f, n_f = _flatten(old), _flatten(new)
    changed = {k: (o_f.get(k), n_f.get(k)) for k in sorted(set(o_f) | set(n_f))
               if o_f.get(k) != n_f.get(k)}
    return YamlDiff(
        rules_added=[r for r in n_rules if r not in o_rules],
        rules_removed=[r for r in o_rules if r not in n_rules],
        rules_modified=[r for r in n_rules
                        if r in o_rules and rule_spec(n_rules[r]) != rule_spec(o_rules[r])],
        fields_changed=changed, old_rules=o_rules, new_rules=n_rules, new_fields=n_f)


@dataclass
class Revision:
    review_id: int
    account: str
    source: str
    strategy_id: str
    ticker: str
    status: str
    proposed_ts: str
    resolved_ts: str | None
    resolution_note: str | None
    is_new: bool
    diff: YamlDiff
    resolver: str = "none"
    gate_rejected: bool = False
    held: bool = False
    hold_reasons: list[str] = field(default_factory=list)
    reversed_by: int | None = None
    rules_added_then_removed: list[str] = field(default_factory=list)
    fields_reverted: list[str] = field(default_factory=list)

    @property
    def proposed_date_et(self) -> str:
        return et_date(self.proposed_ts) or ""

    @property
    def hold_categories(self) -> list[str]:
        return [hold_category(r) for r in self.hold_reasons]

    @property
    def latency_min(self) -> float | None:
        return minutes_between(self.proposed_ts, self.resolved_ts)

    @property
    def applied(self) -> bool:
        return self.status == "approved"


def _observation_ids(conn: sqlite3.Connection) -> tuple[set[int], set[int], dict[int, str]]:
    auto: set[int] = set()
    gate: set[int] = set()
    held: dict[int, str] = {}
    for (text,) in conn.execute(
            "SELECT text FROM observations WHERE source = 'reflection_auto_apply'"):
        if m := _HELD_RE.search(text):
            held[int(m.group(1))] = m.group(2)
        elif m := _AUTO_RE.search(text):
            auto.add(int(m.group(1)))
        elif m := _GATE_RE.search(text):
            gate.add(int(m.group(1)))
    return auto, gate, held


def _hold_column(conn: sqlite3.Connection) -> dict[int, str]:
    """`pending_reviews.hold_reason` where the column exists (added with F1)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_reviews)")}
    if "hold_reason" not in cols:
        return {}
    return {r[0]: r[1] for r in conn.execute(
        "SELECT id, hold_reason FROM pending_reviews WHERE hold_reason IS NOT NULL"
        " AND hold_reason != ''")}


def load_revisions(conn: sqlite3.Connection) -> list[Revision]:
    auto_ids, gate_ids, held_obs = _observation_ids(conn)
    held_col = _hold_column(conn)
    revs: list[Revision] = []
    for r in conn.execute(
            "SELECT id, account, source, strategy_id, ticker, status, ts, resolved_ts,"
            " resolution_note, snapshot FROM pending_reviews"
            " WHERE kind = 'strategy_revision' ORDER BY id"):
        try:
            snap: dict[str, Any] = json.loads(r["snapshot"] or "{}")
        except json.JSONDecodeError:
            snap = {}
        rev = Revision(
            review_id=r["id"], account=r["account"], source=r["source"],
            strategy_id=r["strategy_id"], ticker=r["ticker"], status=r["status"],
            proposed_ts=r["ts"], resolved_ts=r["resolved_ts"],
            resolution_note=r["resolution_note"], is_new=bool(snap.get("is_new")),
            diff=diff_yaml(snap.get("old_yaml") or "", snap.get("new_yaml") or ""))
        if r["status"] == "pending":
            rev.resolver = "none"
        elif r["id"] in auto_ids and r["status"] == "approved":
            rev.resolver = "auto"
        else:
            rev.resolver = "human"
        rev.gate_rejected = r["id"] in gate_ids
        hold_text = held_obs.get(r["id"]) or held_col.get(r["id"])
        if hold_text:
            rev.held = True
            rev.hold_reasons = _split_reasons(hold_text)
        revs.append(rev)
    _mark_reversals(revs)
    return revs


def _mark_reversals(revs: list[Revision]) -> None:
    """For each applied revision, find the first LATER applied revision of the
    same strategy (any source) that removes a rule it added, or puts back a
    rule spec / field value it changed."""
    applied = sorted((r for r in revs if r.applied),
                     key=lambda r: (r.resolved_ts or r.proposed_ts, r.review_id))
    by_strategy: dict[tuple[str, str], list[Revision]] = {}
    for r in applied:
        by_strategy.setdefault((r.account, r.strategy_id), []).append(r)
    for chain in by_strategy.values():
        for i, rev in enumerate(chain):
            added = set(rev.diff.rules_added)
            modified = set(rev.diff.rules_modified)
            fields = dict(rev.diff.fields_changed)
            for later in chain[i + 1:]:
                hit = False
                for rid in sorted(added):
                    if rid not in later.diff.new_rules:
                        rev.rules_added_then_removed.append(rid)
                        added.discard(rid)
                        hit = True
                for rid in sorted(modified):
                    if (rid in later.diff.new_rules and
                            rule_spec(later.diff.new_rules[rid])
                            == rule_spec(rev.diff.old_rules.get(rid))):
                        rev.fields_reverted.append(f"rule:{rid}")
                        modified.discard(rid)
                        hit = True
                for key, (old_val, _new_val) in sorted(fields.items()):
                    if later.diff.new_fields.get(key) == old_val and old_val is not None:
                        rev.fields_reverted.append(key)
                        del fields[key]
                        hit = True
                if hit and rev.reversed_by is None:
                    rev.reversed_by = later.review_id
                if not (added or modified or fields):
                    break


def regime_skips(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(account, ts) of every 'switched off during the run' observation."""
    return [(r["account"], r["ts"]) for r in conn.execute(
        "SELECT account, ts, text FROM observations"
        " WHERE source = 'reflection_auto_apply'") if _REGIME_SKIP_RE.search(r["text"])]
