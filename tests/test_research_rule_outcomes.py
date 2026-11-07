import csv

from allpath_trade.research.cli import main
from allpath_trade.research.db import open_readonly
from allpath_trade.research.rule_outcomes import load_fires, rule_outcome_rows
from tests.research_fixtures import DBBuilder, rule, strategy_yaml

V0 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all")])
V1 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all"),
                          rule("dip", "price < 12")])            # adds dip
V2 = strategy_yaml("s1", [rule("stop", "price < 9", "sell all"),
                          rule("dip", "price < 12")])            # modifies stop
V3 = strategy_yaml("s1", [rule("stop", "price < 9", "sell all")])  # removes dip


def _fire_obs(b, ts, rid, disposition="executed", account="paper"):
    b.observation(ts=ts, source="sentinel", account=account,
                  text=f"s1/{rid} cond -> act: {disposition} detail")


def _db(tmp_path):
    b = DBBuilder(tmp_path / "t.db")
    r_add = b.review(ts="2026-09-01T20:00:00+00:00",
                     resolved_ts="2026-09-01T20:01:00+00:00", old_yaml=V0, new_yaml=V1)
    r_mod = b.review(ts="2026-09-10T20:00:00+00:00",
                     resolved_ts="2026-09-10T20:01:00+00:00", old_yaml=V1, new_yaml=V2)
    r_veto = b.review(ts="2026-09-12T20:00:00+00:00", status="rejected",
                      resolved_ts="2026-09-13T02:00:00+00:00", old_yaml=V2, new_yaml=V3)
    r_rm = b.review(ts="2026-09-20T20:00:00+00:00", source="chat",
                    resolved_ts="2026-09-20T21:00:00+00:00", old_yaml=V2, new_yaml=V3)
    # dip fires twice while live (09-03 via observation only, 09-15 via both
    # rule_fires and observation = one fire), once after removal (impossible
    # in practice; ignored by the window), stop fires on 09-05 (old spec)
    # the observation lands a few seconds AFTER the trade it dispatched
    b.trade(ts="2026-09-03T14:00:00+00:00", reason="strategy s1 rule dip: price < 12 -> buy")
    _fire_obs(b, "2026-09-03T14:00:04+00:00", "dip")
    b.rule_fire(ts="2026-09-15T15:00:00+00:00", strategy_id="s1", rule_id="dip")
    _fire_obs(b, "2026-09-15T15:00:01+00:00", "dip", "queued")
    _fire_obs(b, "2026-09-05T15:00:00+00:00", "stop")
    # a trade with no fire record still counts as a fire (auto+hard, pre-09-28)
    b.trade(ts="2026-09-16T15:00:00+00:00", side="sell",
            reason="strategy s1 rule stop: price < 9 -> sell all")
    b.observation(ts="2026-11-01T00:00:00+00:00", source="consolidator", text="x")
    return b.close(), {"add": r_add, "mod": r_mod, "veto": r_veto, "rm": r_rm}


def _rows(conn, **kw):
    return {(r["review_id"], r["rule_id"], r["change"]): r
            for r in rule_outcome_rows(conn, **kw)}


def test_load_fires_merges_sources(tmp_path):
    path, _ = _db(tmp_path)
    fires = load_fires(open_readonly(path))
    dips = [f for f in fires if f.rule_id == "dip"]
    assert [f.ts[:10] for f in dips] == ["2026-09-03", "2026-09-15"]
    assert dips[1].disposition == "queued"
    stops = [f for f in fires if f.rule_id == "stop"]
    assert [f.ts[:10] for f in stops] == ["2026-09-05", "2026-09-16"]
    assert stops[1].source == "trade"


def test_reflection_rows_and_windows(tmp_path):
    path, ids = _db(tmp_path)
    rows = _rows(open_readonly(path))
    added = rows[(ids["add"], "dip", "added")]
    assert added["applied"] == 1
    assert added["window_kind"] == "live_after"
    assert added["window_end"].startswith("2026-09-20T21:00")  # removed by chat
    assert added["fired"] == 1 and added["n_fires"] == 2
    assert added["first_fire_ts"].startswith("2026-09-03")
    assert added["trade_id"] != "" and added["trade_status"] == "filled"
    assert added["later_removed_by"] == ids["rm"]
    assert added["later_removed_by_source"] == "chat"
    mod = rows[(ids["mod"], "stop", "modified")]
    assert mod["n_fires"] == 1 and mod["first_fire_ts"].startswith("2026-09-16")
    veto = rows[(ids["veto"], "dip", "removed")]
    assert veto["applied"] == 0
    assert veto["window_kind"] == "kept_after_veto"
    assert veto["n_fires"] == 1  # the 09-15 fire happened after the veto
    # chat revisions are excluded by default
    assert all(k[0] != ids["rm"] for k in rows)


def test_all_sources_includes_removal_before_window(tmp_path):
    path, ids = _db(tmp_path)
    rows = _rows(open_readonly(path), sources=None)
    rm = rows[(ids["rm"], "dip", "removed")]
    assert rm["window_kind"] == "before_removal"
    assert rm["window_start"].startswith("2026-09-01T20:01")  # when dip was added
    assert rm["fired"] == 1 and rm["n_fires"] == 2
    assert rm["last_fire_ts"].startswith("2026-09-15")


def test_cli_rule_outcomes(tmp_path):
    path, _ = _db(tmp_path)
    out = tmp_path / "out"
    assert main(["rule-outcomes", "--db", str(path), "--out", str(out)]) == 0
    with (out / "rule_outcomes.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert {r["change"] for r in rows} == {"added", "modified", "removed"}
    assert (out / "rule_outcomes_summary.md").exists()
