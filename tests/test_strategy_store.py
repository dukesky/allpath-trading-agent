import sqlite3
from pathlib import Path

import pytest

from allpath_trade.store.db import connect
from allpath_trade.strategy.loader import StrategyValidationError
from allpath_trade.strategy.model import Authorization, RuleState
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


# --- shadow-dual-active T2: per-account directories + version history -----

def test_same_strategy_id_in_two_account_directories_is_independent(tmp_path):
    # shadow-dual-active T2: the caller (app.py/cli.py) points each
    # account's StrategyStore at its own strategies/{account}/ directory --
    # the store itself does no directory resolution, it just reads/writes
    # whatever `directory` it was given. Two stores pointed at two different
    # directories, each with a strategy file called "a.yaml", must be
    # completely independent: same id, unrelated content, unrelated files.
    conn = connect(tmp_path / "t.db")
    paper_dir = tmp_path / "strategies" / "paper"
    shadow_dir = tmp_path / "strategies" / "shadow"
    paper_dir.mkdir(parents=True)
    shadow_dir.mkdir(parents=True)
    (paper_dir / "a.yaml").write_text(ACTIVE)
    shadow_active = ACTIVE.replace("AAPL", "MSFT")
    (shadow_dir / "a.yaml").write_text(shadow_active)

    paper = StrategyStore(paper_dir, conn, account="paper")
    shadow = StrategyStore(shadow_dir, conn, account="shadow")

    assert paper.load("a").position.ticker == "AAPL"
    assert shadow.load("a").position.ticker == "MSFT"

    # Writing rule state for "a" in one account's file must never touch the
    # other's -- separate directories, separate files, on top of the
    # already-independent (account, strategy_id, rule_id) rows.
    paper.set_rule_state("a", "r1", RuleState.TRIGGERED)
    assert paper.load("a").rules[0].state == RuleState.TRIGGERED
    assert shadow.load("a").rules[0].state == RuleState.ARMED
    assert (paper_dir / "a.yaml").read_text() == ACTIVE  # untouched by set_rule_state
    assert (shadow_dir / "a.yaml").read_text() == shadow_active


def test_versions_scoped_to_account(tmp_path):
    # shadow-dual-active T2 (carried from T1 review): strategy_versions
    # gained an `account` column -- the same strategy id snapshotted from
    # two different StrategyStore accounts must keep two fully independent
    # version histories, not one shared list keyed on strategy_id alone.
    (tmp_path / "a.yaml").write_text(ACTIVE)
    conn = connect(tmp_path / "t.db")
    paper = StrategyStore(tmp_path, conn, account="paper")
    shadow = StrategyStore(tmp_path, conn, account="shadow")

    doc = paper.load("a")
    paper.snapshot_version(doc, reason="paper initial")
    paper.snapshot_version(doc.model_copy(update={"version": 2}), reason="paper v2")

    shadow.snapshot_version(doc, reason="shadow initial")

    paper_versions = paper.versions("a")
    shadow_versions = shadow.versions("a")

    assert [v["version"] for v in paper_versions] == [2, 1]
    assert [r["reason"] for r in paper_versions] == ["paper v2", "paper initial"]
    assert [v["version"] for v in shadow_versions] == [1]
    assert shadow_versions[0]["reason"] == "shadow initial"

    # And the account column itself is stamped correctly on every row.
    rows = list(conn.execute(
        "SELECT account, reason FROM strategy_versions ORDER BY id"))
    assert [(r["account"], r["reason"]) for r in rows] == [
        ("paper", "paper initial"), ("paper", "paper v2"), ("shadow", "shadow initial")]


def test_set_authorization_rewrites_only_that_field(tmp_path):
    # arrange: write a strategy YAML with authorization: auto, version 3
    s1_yaml = """
name: "S1"
status: active
version: 3
authorization: auto
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""
    (tmp_path / "s1.yaml").write_text(s1_yaml)
    store = StrategyStore(tmp_path, connect(tmp_path / "t.db"))

    store.set_authorization("s1", Authorization.CONFIRM, "drawdown breaker")
    doc = store.load("s1")
    assert doc.authorization == Authorization.CONFIRM
    assert doc.version == 3            # untouched
    assert doc.status.value == "active"  # untouched
    versions = store.versions("s1")
    assert versions[-1]["reason"] == "drawdown breaker"  # match the actual
    # column name used by snapshot_version/versions in this file's tests
