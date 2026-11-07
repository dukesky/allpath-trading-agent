import sqlite3

import pytest

from allpath_trade.research.db import open_readonly
from allpath_trade.research.revisions import diff_yaml, load_revisions
from tests.research_fixtures import DBBuilder, rule, strategy_yaml


def test_open_readonly_refuses_writes(tmp_path):
    path = DBBuilder(tmp_path / "t.db").close()
    conn = open_readonly(path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO app_state (key, value) VALUES ('x', 'y')")


def test_open_readonly_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        open_readonly(tmp_path / "nope.db")


def test_diff_yaml_rules_and_fields():
    old = strategy_yaml("s1", [rule("a", "price < 10"), rule("b", "price > 20"),
                               rule("c", "price > 30")], target_weight="0.05")
    new = strategy_yaml("s1", [rule("a", "price < 10", state="triggered"),
                               rule("b", "price > 25"), rule("d", "price < 5")],
                        target_weight="0.08")
    d = diff_yaml(old, new)
    assert d.rules_added == ["d"]
    assert d.rules_removed == ["c"]
    assert d.rules_modified == ["b"]  # a's state change is not a modification
    assert d.fields_changed == {"position.target_weight": ("0.05", "0.08")}
    assert d.old_rules["b"]["condition"] == "price > 20"
    assert d.new_rules["d"]["condition"] == "price < 5"


def test_diff_yaml_new_strategy_adds_all_rules():
    d = diff_yaml("", strategy_yaml("s1", [rule("a", "price < 10")]))
    assert d.rules_added == ["a"]
    assert d.rules_removed == []


def test_load_revisions_resolver_and_reversals(tmp_path):
    b = DBBuilder(tmp_path / "t.db")
    v1 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all")])
    v2 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all"),
                              rule("dip", "price < 12")], target_weight="0.08")
    v3 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all")],
                       target_weight="0.05")
    # human-approved (latency hours), auto-applied (observation), vetoed
    r1 = b.review(ts="2026-08-12T20:00:00+00:00", resolved_ts="2026-08-13T04:00:00+00:00",
                  old_yaml=v1, new_yaml=v2)
    r2 = b.review(ts="2026-08-28T20:00:00+00:00", resolved_ts="2026-08-28T20:01:30+00:00",
                  old_yaml=v2, new_yaml=v3)
    r3 = b.review(ts="2026-08-29T20:00:00+00:00", status="rejected",
                  resolved_ts="2026-08-30T01:00:00+00:00", old_yaml=v3, new_yaml=v2)
    b.observation(ts="2026-08-28T20:01:30+00:00", source="reflection_auto_apply",
                  text=f"revision #{r2} for s1 auto-applied (experiment mode).")
    b.review(ts="2026-08-28T20:00:00+00:00", kind="order", source="sentinel",
             rule_type="hard", rule_id="dip")
    revs = {r.review_id: r for r in load_revisions(open_readonly(b.close()))}
    assert set(revs) == {r1, r2, r3}  # orders are not revisions
    assert revs[r1].resolver == "human"
    assert revs[r2].resolver == "auto"
    assert revs[r3].resolver == "human" and revs[r3].status == "rejected"
    assert revs[r1].latency_min == pytest.approx(480.0)
    assert revs[r1].proposed_date_et == "2026-08-12"
    # r1 added `dip` and moved target_weight; r2 removed `dip` and moved it back
    assert revs[r1].reversed_by == r2
    assert revs[r1].rules_added_then_removed == ["dip"]
    assert revs[r1].fields_reverted == ["position.target_weight"]
    # a rejected revision never reverses anything and is never reversed
    assert revs[r2].reversed_by is None
    assert revs[r3].reversed_by is None


def test_gate_rejection_observation_is_flagged(tmp_path):
    b = DBBuilder(tmp_path / "t.db")
    v1 = strategy_yaml("s1", [rule("a", "price < 10")])
    rid = b.review(ts="2026-12-01T21:00:00+00:00", status="pending",
                   old_yaml=v1, new_yaml=v1)
    b.observation(ts="2026-12-01T21:01:00+00:00", source="reflection_auto_apply",
                  text=f"revision #{rid} for s1 NOT auto-applied (frozen field "
                       "authorization); left pending for human review")
    rev = load_revisions(open_readonly(b.close()))[0]
    assert rev.gate_rejected is True
    assert rev.resolver == "none"


def test_modified_rule_reverted_counts(tmp_path):
    b = DBBuilder(tmp_path / "t.db")
    v1 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all")])
    v2 = strategy_yaml("s1", [rule("stop", "price < 8", "sell all")])
    r1 = b.review(ts="2026-09-01T20:00:00+00:00", resolved_ts="2026-09-01T20:01:00+00:00",
                  old_yaml=v1, new_yaml=v2)
    r2 = b.review(ts="2026-09-05T20:00:00+00:00", source="chat",
                  resolved_ts="2026-09-05T20:02:00+00:00", old_yaml=v2, new_yaml=v1)
    revs = {r.review_id: r for r in load_revisions(open_readonly(b.close()))}
    assert revs[r1].reversed_by == r2
    assert revs[r1].fields_reverted == ["rule:stop"]


HELD_TEXT = ("revision #{rid} for s1 held for human: changes position.ticker (NVDA -> "
             "AMD); removes sell rule stop; loosens buy rule dip beyond 5% (price < 12 "
             "-> price < 13). Not auto-applied (auto-apply allow-list); left pending")


def test_held_revision_is_classified_with_reasons(tmp_path):
    b = DBBuilder(tmp_path / "t.db")
    v1 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all")])
    held = b.review(ts="2026-12-02T21:00:00+00:00", status="pending",
                    old_yaml=v1, new_yaml=v1)
    held_then_ok = b.review(ts="2026-12-03T21:00:00+00:00",
                            resolved_ts="2026-12-04T15:00:00+00:00",
                            old_yaml=v1, new_yaml=v1)
    auto = b.review(ts="2026-12-03T21:00:00+00:00",
                    resolved_ts="2026-12-03T21:01:00+00:00", old_yaml=v1, new_yaml=v1)
    b.observation(ts="2026-12-02T21:01:00+00:00", source="reflection_auto_apply",
                  text=HELD_TEXT.format(rid=held))
    b.observation(ts="2026-12-03T21:01:00+00:00", source="reflection_auto_apply",
                  text=f"revision #{held_then_ok} for s1 held for human: proposes a new "
                       "strategy. Not auto-applied (auto-apply allow-list); left pending")
    b.observation(ts="2026-12-03T21:01:00+00:00", source="reflection_auto_apply",
                  text=f"revision #{auto} for s1 auto-applied (experiment mode).")
    revs = {r.review_id: r for r in load_revisions(open_readonly(b.close()))}
    h = revs[held]
    assert h.held is True and h.resolver == "none" and h.gate_rejected is False
    assert h.hold_reasons == ["changes position.ticker (NVDA -> AMD)",
                              "removes sell rule stop",
                              "loosens buy rule dip beyond 5% (price < 12 -> price < 13)"]
    assert h.hold_categories == ["retarget_ticker", "removes_exit", "loosens_entry"]
    # held, then a human approved it: held AND human-decided, never auto
    assert revs[held_then_ok].held is True
    assert revs[held_then_ok].resolver == "human"
    assert revs[held_then_ok].hold_categories == ["new_strategy"]
    assert revs[auto].held is False and revs[auto].resolver == "auto"


def test_hold_reason_column_is_a_fallback(tmp_path):
    b = DBBuilder(tmp_path / "t.db")
    v1 = strategy_yaml("s1", [rule("stop", "price < 10", "sell all")])
    rid = b.review(ts="2026-12-02T21:00:00+00:00", status="pending",
                   old_yaml=v1, new_yaml=v1)
    b.conn.execute("UPDATE pending_reviews SET hold_reason = ? WHERE id = ?",
                   ("adds rule extra; raises position.max_weight (0.1 -> 0.2)", rid))
    b.conn.commit()
    rev = load_revisions(open_readonly(b.close()))[0]
    assert rev.held is True
    assert rev.hold_categories == ["adds_rule", "size_increase"]


@pytest.mark.parametrize(("reason", "category"), [
    ("proposes a new strategy", "new_strategy"),
    ("proposal does not parse (bad yaml)", "unparseable"),
    ("could not classify the revision (boom)", "unparseable"),
    ("changes position.ticker (A -> B)", "retarget_ticker"),
    ("sets position.max_value", "size_increase"),
    ("removes position.max_weight", "size_increase"),
    ("raises position.target_weight (0.05 -> 0.08)", "size_increase"),
    ("removes sell rule stop", "removes_exit"),
    ("adds rule r9", "adds_rule"),
    ("changes authorization", "frozen_field"),
    ("changes status", "frozen_field"),
    ("turns off notify_email", "notify_off"),
    ("changes stop from sell to buy", "flips_side"),
    ("changes sell rule stop type hard -> soft", "rule_type"),
    ("makes buy rule dip fire more often (rearm)", "rearm"),
    ("makes sell rule stop fire less often (rearm)", "rearm"),
    ("sells less on sell rule stop", "loosens_exit"),
    ("loosens sell rule stop (price < 10 -> price < 8)", "loosens_exit"),
    ("loosens buy rule dip beyond 5% (price < 12 -> price < 13)", "loosens_entry"),
    ("buys more on buy rule dip ($1000 -> $3000)", "buys_more"),
    ("changes the condition structure of sell rule stop", "condition_rewrite"),
    ("moves an equality in buy rule dip", "condition_rewrite"),
    ("could not read the condition of buy rule dip (x)", "unparseable"),
    ("changes the action of buy rule dip", "changes_action"),
    ("disables sell rule stop", "disables_rule"),
    ("re-arms buy rule dip", "rearms_rule"),
    ("changes options", "other_field"),
    ("something new", "other"),
])
def test_hold_category(reason, category):
    from allpath_trade.research.revisions import hold_category
    assert hold_category(reason) == category
