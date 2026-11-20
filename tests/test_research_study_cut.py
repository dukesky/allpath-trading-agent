"""`--shadow-study-end` (allpath_trade/research/study_cut.py): every
`allpath-research` export drops shadow-account rows after the moment the
shadow account left the study; paper rows are never dropped."""

from __future__ import annotations

import csv
from datetime import UTC, datetime

import pytest

from allpath_trade.research import study_cut
from allpath_trade.research.cli import build_parser, main
from allpath_trade.store.db import connect

CUT = study_cut.parse_cut(study_cut.DEFAULT_SHADOW_STUDY_END)


def test_default_cut_is_the_reconciliation_moment():
    assert CUT == datetime(2026, 11, 20, 21, 27, 59, tzinfo=UTC)
    for command in ("regime-metrics", "rule-outcomes", "replay-rules"):
        args = build_parser().parse_args([command, "--db", "x", "--out", "y"])
        assert args.shadow_study_end == "2026-11-20T21:27:59Z"
    assert study_cut.parse_cut("none") is None and study_cut.parse_cut("") is None
    assert study_cut.parse_cut("2026-11-20") == datetime(2026, 11, 20, 23, 59, 59, tzinfo=UTC)
    with pytest.raises(ValueError):
        study_cut.parse_cut("last week")


def test_only_shadow_rows_after_the_cut_are_dropped():
    rows = [
        {"account": "shadow", "ts": "2026-11-20T21:27:59+00:00"},          # at the cut: kept
        {"account": "shadow", "ts": "2026-11-20T21:28:00+00:00"},          # after: dropped
        {"account": "paper", "ts": "2026-12-15T15:00:00+00:00"},           # paper: always kept
        {"account": "shadow", "date": "2026-11-20"},                       # the cut's ET day: kept
        {"account": "shadow", "date": "2026-11-21"},                       # dropped
        {"account": "paper", "date": "2026-11-21"},
        {"account": "shadow", "proposed_ts": "2026-11-25T01:00:00+00:00"}, # dropped
        {"account": "shadow", "decision_ts": "2026-11-01T01:00:00Z", "decision_date": "x"},
        {"purpose": "reflection:shadow", "date": "2026-11-23"},            # dropped
        {"purpose": "reflection:paper", "date": "2026-11-23"},
        {"account": "shadow"},                                             # undated: kept
        {"date": "2026-12-01", "tier": "chat"},                            # no account: kept
    ]
    kept, dropped = study_cut.apply(rows, CUT)
    assert dropped == 4
    assert [rows.index(r) for r in kept] == [0, 2, 3, 5, 7, 9, 10, 11]
    assert study_cut.apply(rows, None) == (rows, 0)


def _db(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    for account in ("paper", "shadow"):
        for ts in ("2026-11-19T15:00:00+00:00", "2026-11-24T15:00:00+00:00"):
            conn.execute(
                "INSERT INTO pending_reviews (account, ts, strategy_id, rule_id, ticker,"
                " rule_type, condition, action, snapshot, status, kind, source)"
                " VALUES (?, ?, 's', 'r', 'ZZZ', 'soft', 'price < 1', 'buy $1', '{}',"
                " 'pending', 'order', 'sentinel')", (account, ts))
    conn.commit()
    conn.close()
    return path


def _rows(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def test_regime_metrics_export_drops_shadow_rows_after_the_cut(tmp_path, capsys):
    db = _db(tmp_path)
    out = tmp_path / "cut"
    assert main(["regime-metrics", "--db", str(db), "--from", "2026-11-17",
                 "--to", "2026-11-27", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "shadow study end 2026-11-20T21:27:59+00:00: dropped" in printed
    cards = {(r["account"], r["date_et"]) for r in _rows(out / "card_formats.csv")}
    assert cards == {("paper", "2026-11-19"), ("paper", "2026-11-24"), ("shadow", "2026-11-19")}
    daily = _rows(out / "regime_metrics_daily.csv")
    assert max(r["date"] for r in daily if r["account"] == "shadow") == "2026-11-20"
    assert max(r["date"] for r in daily if r["account"] == "paper") == "2026-11-27"

    everything = tmp_path / "all"
    assert main(["regime-metrics", "--db", str(db), "--from", "2026-11-17", "--to",
                 "2026-11-27", "--out", str(everything), "--shadow-study-end", "none"]) == 0
    assert "none (all shadow rows kept)" in capsys.readouterr().out
    assert len(_rows(everything / "card_formats.csv")) == 4
    full = _rows(everything / "regime_metrics_daily.csv")
    assert max(r["date"] for r in full if r["account"] == "shadow") == "2026-11-27"
    # Paper rows are the same with and without the cut.
    assert [r for r in daily if r["account"] == "paper"] == [
        r for r in full if r["account"] == "paper"]


def test_rule_outcomes_export_takes_the_option(tmp_path, capsys):
    db = _db(tmp_path)
    assert main(["rule-outcomes", "--db", str(db), "--out", str(tmp_path / "ro")]) == 0
    assert "shadow study end 2026-11-20T21:27:59+00:00" in capsys.readouterr().out
