import csv
import hashlib
import json

import pytest

from allpath_trade.research.cli import main
from allpath_trade.research.db import open_readonly
from allpath_trade.research.regime_metrics import daily_rows, window_for
from tests.research_fixtures import DBBuilder, rule, strategy_yaml


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path):
    with path.open() as f:
        return list(csv.DictReader(f))


@pytest.fixture()
def db(tmp_path):
    b = DBBuilder(tmp_path / "prod.db")
    v1 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all")])
    v2 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all"),
                              rule("dip", "price < 12")])
    # A: one human-approved and one vetoed reflection revision on 08-12 (ET)
    b.report(date="2026-08-12")
    b.review(ts="2026-08-12T20:10:00+00:00", resolved_ts="2026-08-13T02:10:00+00:00",
             old_yaml=v1, new_yaml=v2)
    b.review(ts="2026-08-12T20:11:00+00:00", status="rejected",
             resolved_ts="2026-08-12T22:11:00+00:00", old_yaml=v2, new_yaml=v1)
    # B: 09-01 two auto-applied, one of which removes the rule added on 08-12
    b.report(date="2026-09-01", tokens=1234)
    a1 = b.review(ts="2026-09-01T20:10:00+00:00", resolved_ts="2026-09-01T20:11:00+00:00",
                  old_yaml=v2, new_yaml=v1)
    a2 = b.review(ts="2026-09-01T20:10:30+00:00", resolved_ts="2026-09-01T20:12:00+00:00",
                  strategy_id="s2", old_yaml="", new_yaml=strategy_yaml("s2", []),
                  is_new=True)
    for rid in (a1, a2):
        b.observation(ts="2026-09-01T20:12:00+00:00", source="reflection_auto_apply",
                      text=f"revision #{rid} for sX auto-applied (experiment mode).")
    # a late-evening UTC proposal that is still 09-01 in ET
    b.review(ts="2026-09-02T01:30:00+00:00", source="chat",
             resolved_ts="2026-09-02T01:31:00+00:00", old_yaml=v1, new_yaml=v1)
    # orders on 09-01: one human-approved review that filled, one auto fill,
    # one gate rejection, one rejected review
    b.review(ts="2026-09-01T14:00:00+00:00", kind="order", source="sentinel",
             rule_type="hard", rule_id="dip", resolved_ts="2026-09-01T15:00:00+00:00",
             execution_result=json.dumps({"submitted": True, "order": {"id": "ord-1"}}))
    b.review(ts="2026-09-01T16:00:00+00:00", kind="order", source="sentinel",
             rule_type="hard", rule_id="dip", status="rejected",
             resolved_ts="2026-09-01T16:30:00+00:00")
    b.trade(ts="2026-09-01T15:00:01+00:00", broker_order_id="ord-1",
            reason="strategy s1 rule dip: price < 12 -> buy $1000")
    b.trade(ts="2026-09-01T17:00:00+00:00", broker_order_id="ord-2",
            reason="strategy s1 rule stop: price < 10 -> sell all", side="sell")
    b.trade(ts="2026-09-01T18:00:00+00:00", status="rejected",
            reason="strategy s1 rule dip: price < 12 -> buy $1000")
    # breaker trip on 11-05, reset 11-06 22:28Z (known reset list)
    b.observation(ts="2026-11-05T15:46:22+00:00", source="breaker",
                  text="drawdown breaker tripped: 15.2% below peak 1; demoted: none")
    # strategy authorization history for paper
    b.version(ts="2026-08-01T00:00:00+00:00", strategy_id="s1", version=1, content=v1)
    b.version(ts="2026-09-14T15:00:00+00:00", strategy_id="s1", version=2,
              content=strategy_yaml("s1", [], authorization="confirm"))
    b.regime_event(ts="2026-10-26T03:46:41+00:00", regime="human")
    b.llm_usage(ts="2026-09-01T20:05:00+00:00", tier="memory", model="opus")
    b.llm_usage(ts="2026-09-01T20:06:00+00:00", tier="memory", model="opus",
                purpose="reflection:paper", inp=500, out=50)
    b.llm_failure(ts="2026-11-03T21:00:00+00:00", tier="memory")
    return b.close()


def _row(rows, date, account="paper"):
    [r] = [r for r in rows if r["date"] == date and r["account"] == account]
    return r


def test_window_for():
    assert window_for("2026-08-27") == "A"
    assert window_for("2026-08-28") == "B"
    assert window_for("2026-10-10") == ""
    assert window_for("2026-11-30") == "A'"
    assert window_for("2026-12-01") == "B'"


def test_daily_rows_counts(db):
    rows = daily_rows(open_readonly(db), "2026-08-10", "2026-11-07")
    a = _row(rows, "2026-08-12")
    assert a["window"] == "A"
    assert a["report_status"] == "ok"
    assert a["prop_reflection_revision"] == 2
    assert a["refl_rev_human_approved"] == 1
    assert a["refl_rev_vetoed"] == 1
    assert a["refl_rev_auto_applied"] == 0
    assert a["refl_rev_latency_min_median"] == pytest.approx(240.0)
    b = _row(rows, "2026-09-01")
    assert b["window"] == "B"
    assert b["refl_rev_auto_applied"] == 2
    assert b["refl_rev_gate_rejections"] == 0
    assert b["prop_chat_revision"] == 1  # 01:30Z on 09-02 is 09-01 ET
    assert b["report_tokens"] == 1234
    assert b["reflection_tokens_in"] == 500
    assert b["order_reviews_proposed"] == 2
    assert b["order_reviews_approved"] == 1
    assert b["order_reviews_rejected"] == 1
    assert b["orders_auto_executed"] == 1
    assert b["trades_filled"] == 2
    assert b["trades_rejected"] == 1
    assert b["models_memory"] == "opus"
    # carried forward on a day with no calls; a failed call names its model too
    assert _row(rows, "2026-09-02")["models_memory"] == "opus"
    assert _row(rows, "2026-11-03")["models_memory"] == "m"
    assert _row(rows, "2026-08-12")["models_memory"] == ""
    # the 08-12 human approval was undone by the 09-01 auto-apply
    assert a["rev_applied_later_reversed"] == 1
    assert a["rev_rules_added_then_removed"] == 1
    # authorization history: auto in Aug, confirm after 09-14
    assert a["strat_active_auto"] == 1 and a["strat_active_confirm"] == 0
    assert _row(rows, "2026-09-15")["strat_active_confirm"] == 1
    assert _row(rows, "2026-11-05")["breaker_state_eod"] == "tripped"
    assert _row(rows, "2026-11-05")["breaker_trips"] == 1
    assert _row(rows, "2026-11-06")["breaker_state_eod"] == "ok"
    assert _row(rows, "2026-10-26")["regime_db"] == "human"
    assert _row(rows, "2026-10-23")["regime_db"] == ""
    assert _row(rows, "2026-08-13")["report_status"] == "none"


def test_date_range_filters_and_weekends(db):
    rows = daily_rows(open_readonly(db), "2026-08-12", "2026-08-16")
    dates = sorted({r["date"] for r in rows})
    # 08-15/08-16 are a weekend with no activity: skipped
    assert dates == ["2026-08-12", "2026-08-13", "2026-08-14"]
    assert {r["account"] for r in rows} == {"paper", "shadow"}


def test_cli_writes_csvs_and_leaves_db_untouched(db, tmp_path):
    before = _sha(db)
    out = tmp_path / "out"
    rc = main(["regime-metrics", "--db", str(db), "--from", "2026-08-10",
               "--to", "2026-11-07", "--out", str(out)])
    assert rc == 0
    assert _sha(db) == before
    daily = _read(out / "regime_metrics_daily.csv")
    assert _row(daily, "2026-09-01")["refl_rev_auto_applied"] == "2"
    revs = _read(out / "revisions.csv")
    assert len(revs) == 5
    assert {r["resolver"] for r in revs} == {"human", "auto"}
    orders = _read(out / "orders.csv")
    paths = sorted(r["path"] for r in orders)
    assert paths == ["auto", "gate_rejected", "review_approved", "review_rejected"]
    summary = (out / "summary.md").read_text()
    assert "| A | paper |" in summary
    assert (out / "llm_daily.csv").exists()
    assert (out / "llm_failures_daily.csv").exists()


def test_cli_rejects_missing_db(tmp_path, capsys):
    rc = main(["regime-metrics", "--db", str(tmp_path / "x.db"), "--out", str(tmp_path)])
    assert rc == 2
