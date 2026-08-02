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
