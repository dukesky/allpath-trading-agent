import sqlite3
from pathlib import Path

import pytest

from allpath_trade.store.db import connect
from allpath_trade.strategy.loader import StrategyValidationError
from allpath_trade.strategy.model import RuleState
from allpath_trade.strategy.store import StrategyStore

ACTIVE = """
name: "A"
status: active
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""
DRAFT = """
name: "B"
status: draft
position: {ticker: MSFT, target_weight: 10%}
"""


@pytest.fixture()
def store(tmp_path: Path) -> StrategyStore:
    (tmp_path / "a.yaml").write_text(ACTIVE)
    (tmp_path / "b.yaml").write_text(DRAFT)
    return StrategyStore(tmp_path, connect(tmp_path / "t.db"))


def test_load_all_filters_active(store):
    docs = store.load_all()
    assert [d.id for d in docs] == ["a"]
    assert store.load_all(status=None).__len__() == 2


def test_rule_state_merge_and_rearm(store):
    store.set_rule_state("a", "r1", RuleState.TRIGGERED)
    [doc] = store.load_all()
    assert doc.rules[0].state == RuleState.TRIGGERED
    store.rearm("a", "r1")
    assert store.load("a").rules[0].state == RuleState.ARMED


def test_snapshot_and_versions(store):
    doc = store.load("a")
    store.snapshot_version(doc, reason="initial")
    doc2 = doc.model_copy(update={"version": 2})
    store.snapshot_version(doc2, reason="tighten stop")
    rows = store.versions("a")
    assert [r["version"] for r in rows] == [2, 1]
    assert "AAPL" in rows[0]["content"]


def test_invalid_file_is_skipped_without_errors_param(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: x\nstatus: active\n")
    s = StrategyStore(tmp_path, connect(tmp_path / "t.db"))
    docs = s.load_all()  # must not raise: one bad file must not halt monitoring
    assert docs == []


def test_invalid_file_is_skipped_and_collected_good_still_loads(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: x\nstatus: active\n")
    (tmp_path / "a.yaml").write_text(ACTIVE)
    s = StrategyStore(tmp_path, connect(tmp_path / "t.db"))
    errors: list[str] = []
    docs = s.load_all(status=None, errors=errors)
    assert [d.id for d in docs] == ["a"]
    assert len(errors) == 1
    assert "bad.yaml" in errors[0]


def test_load_still_raises_for_invalid_file(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: x\nstatus: active\n")
    s = StrategyStore(tmp_path, connect(tmp_path / "t.db"))
    with pytest.raises(StrategyValidationError):
        s.load("bad")


# --- shadow-dual-active T1: account scoping (rule_states) ------------------

def test_rule_state_isolated_per_account(tmp_path):
    # Same strategy id, same rule id, different accounts -- Task 2 gives
    # each account its own strategy directory (so "a" can legitimately
    # exist in both), and each account's armed/triggered state must be
    # fully independent.
    (tmp_path / "a.yaml").write_text(ACTIVE)
    conn = connect(tmp_path / "t.db")
    paper = StrategyStore(tmp_path, conn)
    shadow = StrategyStore(tmp_path, conn, account="shadow")

    paper.set_rule_state("a", "r1", RuleState.TRIGGERED)

    assert paper.load("a").rules[0].state == RuleState.TRIGGERED
    # shadow's instance must still see the rule's default ARMED state --
    # paper's write must not have landed in shadow's row.
    assert shadow.load("a").rules[0].state == RuleState.ARMED

    shadow.set_rule_state("a", "r1", RuleState.DISABLED)
    assert paper.load("a").rules[0].state == RuleState.TRIGGERED
    assert shadow.load("a").rules[0].state == RuleState.DISABLED

    rows = list(conn.execute(
        "SELECT account, state FROM rule_states WHERE strategy_id = 'a'"
        " ORDER BY account"))
    assert [(r["account"], r["state"]) for r in rows] == [
        ("paper", "triggered"), ("shadow", "disabled")]


def test_rearm_scoped_to_account(tmp_path):
    (tmp_path / "a.yaml").write_text(ACTIVE)
    conn = connect(tmp_path / "t.db")
    paper = StrategyStore(tmp_path, conn)
    shadow = StrategyStore(tmp_path, conn, account="shadow")
    paper.set_rule_state("a", "r1", RuleState.TRIGGERED)
    shadow.set_rule_state("a", "r1", RuleState.TRIGGERED)

    paper.rearm("a", "r1")
    assert paper.load("a").rules[0].state == RuleState.ARMED
    assert shadow.load("a").rules[0].state == RuleState.TRIGGERED


def test_legacy_rule_states_row_defaults_account_paper_after_migration(tmp_path):
    # Simulate a pre-shadow-dual-active database: `rule_states` exists with
    # the old 2-column PRIMARY KEY (strategy_id, rule_id), no `account`
    # column. The migration must add + backfill `account` AND rebuild the
    # table so the PK becomes (account, strategy_id, rule_id) -- SQLite
    # can't ALTER a PRIMARY KEY in place.
    (tmp_path / "a.yaml").write_text(ACTIVE)
    path = tmp_path / "t.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE rule_states (strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " state TEXT NOT NULL, updated_ts TEXT NOT NULL,"
        " PRIMARY KEY (strategy_id, rule_id))")
    raw.execute(
        "INSERT INTO rule_states (strategy_id, rule_id, state, updated_ts)"
        " VALUES ('a', 'r1', 'triggered', '2020-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT account FROM rule_states").fetchone()
    assert row["account"] == "paper"

    paper = StrategyStore(tmp_path, conn)
    assert paper.load("a").rules[0].state == RuleState.TRIGGERED

    # The rebuilt table must genuinely enforce the new composite PK -- a
    # shadow row for the same (strategy_id, rule_id) must be independent,
    # not a PK collision with the legacy paper row.
    shadow = StrategyStore(tmp_path, conn, account="shadow")
    shadow.set_rule_state("a", "r1", RuleState.DISABLED)
    assert paper.load("a").rules[0].state == RuleState.TRIGGERED
    assert shadow.load("a").rules[0].state == RuleState.DISABLED

    # Idempotent across restarts.
    conn2 = connect(path)
    row2 = conn2.execute(
        "SELECT account FROM rule_states WHERE rule_id = 'r1' AND account = 'paper'"
    ).fetchone()
    assert row2["account"] == "paper"
