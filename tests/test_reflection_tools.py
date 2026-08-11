import pytest

from allpath_trade.agent.reflection_tools import (
    apply_revision_factory,
    register_reflection_tools,
)
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import ToolCall
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError
from allpath_trade.strategy.store import StrategyStore

CURRENT = """\
name: "S1"
status: active
version: 1
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""

PROPOSED = """\
name: "S1"
status: active
version: 2
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 90", action: "sell all"}
"""


def make(tmp_path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "s1.yaml").write_text(CURRENT)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(strategies_dir, conn)
    queue = ReviewQueue(conn, executor=None)
    reg = ToolRegistry()
    register_reflection_tools(reg, strategies=store, queue=queue)
    return reg, store, queue


def call(reg, **kw):
    return reg.execute(ToolCall(id="x", name="propose_strategy_revision", arguments=kw))


def test_specs_lists_the_tool(tmp_path):
    reg, _, _ = make(tmp_path)
    names = {s.name for s in reg.specs()}
    assert names == {"propose_strategy_revision"}


def test_valid_proposal_queues_and_returns_pending_text(tmp_path):
    reg, _, queue = make(tmp_path)
    out = call(reg, strategy_id="s1", new_yaml=PROPOSED, rationale="stop was too loose")
    rows = queue.list()
    assert len(rows) == 1
    rid = rows[0]["id"]
    assert out == (f"Revision queued for user approval (#{rid}). It will not "
                   "take effect unless approved.")
    assert rows[0]["kind"] == "strategy_revision"
    assert rows[0]["strategy_id"] == "s1"
    assert rows[0]["ticker"] == "AAPL"


def test_id_change_is_rejected_and_queue_stays_empty(tmp_path):
    reg, _, queue = make(tmp_path)
    bad = PROPOSED.replace('name: "S1"', 'id: other-strategy\nname: "S1"')
    out = call(reg, strategy_id="s1", new_yaml=bad, rationale="sneaky id swap")
    assert out.startswith("error:")
    assert "id" in out
    assert queue.list() == []


def test_invalid_yaml_returns_error_string_queue_untouched(tmp_path):
    reg, _, queue = make(tmp_path)
    out = call(reg, strategy_id="s1", new_yaml="not: [valid", rationale="r")
    assert out.startswith("error:")
    assert queue.list() == []


def test_yaml_that_fails_strategy_validation_returns_error_string(tmp_path):
    reg, _, queue = make(tmp_path)
    # Missing required `position` field.
    out = call(reg, strategy_id="s1", new_yaml="name: Bad\nstatus: active\n",
               rationale="r")
    assert out.startswith("error:")
    assert queue.list() == []


def test_missing_strategy_returns_error_string(tmp_path):
    reg, _, queue = make(tmp_path)
    out = call(reg, strategy_id="does-not-exist", new_yaml=PROPOSED, rationale="r")
    assert out.startswith("error:")
    assert "not found" in out
    assert queue.list() == []


def test_invalid_strategy_id_returns_error_string(tmp_path):
    reg, _, queue = make(tmp_path)
    out = call(reg, strategy_id="../../etc/passwd", new_yaml=PROPOSED, rationale="r")
    assert out.startswith("error:") and "invalid strategy id" in out
    assert queue.list() == []


def test_empty_rationale_is_rejected(tmp_path):
    reg, _, queue = make(tmp_path)
    out = call(reg, strategy_id="s1", new_yaml=PROPOSED, rationale="   ")
    assert out.startswith("error:") and "rationale" in out
    assert queue.list() == []


def test_rationale_is_capped_at_2000_chars(tmp_path):
    reg, _, queue = make(tmp_path)
    long_rationale = "x" * 3000
    call(reg, strategy_id="s1", new_yaml=PROPOSED, rationale=long_rationale)
    row = queue.list()[0]
    import json

    snapshot = json.loads(row["snapshot"])
    assert len(snapshot["rationale"]) == 2000


def test_diff_reflects_old_and_new_yaml(tmp_path):
    reg, _, queue = make(tmp_path)
    call(reg, strategy_id="s1", new_yaml=PROPOSED, rationale="tighten stop")
    import json

    row = queue.list()[0]
    snapshot = json.loads(row["snapshot"])
    assert "-  - {id: r1, type: hard, condition: \"price < 100\", action: \"sell all\"}" \
        in snapshot["diff"]
    assert "+  - {id: r1, type: hard, condition: \"price < 90\", action: \"sell all\"}" \
        in snapshot["diff"]


# --- applier -----------------------------------------------------------

def test_applier_writes_atomically_and_snapshots(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(tmp_path / "strategies", store)
    apply_fn("s1", PROPOSED)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == PROPOSED
    versions = store.versions("s1")
    assert versions[0]["reason"] == "reflection revision approved via web"
    assert versions[0]["version"] == 2


def test_applier_raises_revision_validation_error_on_invalid_content(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(tmp_path / "strategies", store)
    with pytest.raises(RevisionValidationError):
        apply_fn("s1", "name: Bad\nstatus: active\n")  # missing `position`
    # nothing written: the file is untouched
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


def test_applier_wired_through_queue_approve_rolls_back_to_pending(tmp_path):
    _, store, queue = make(tmp_path)
    apply_fn = apply_revision_factory(tmp_path / "strategies", store)
    queue.set_revision_applier(apply_fn)
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT,
        new_yaml="name: Bad\nstatus: active\n", diff="d", rationale="r")

    with pytest.raises(RevisionValidationError):
        queue.approve(rid)

    row = queue.get(rid)
    assert row["status"] == "pending"
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


def test_applier_wired_through_queue_approve_succeeds(tmp_path):
    _, store, queue = make(tmp_path)
    apply_fn = apply_revision_factory(tmp_path / "strategies", store)
    queue.set_revision_applier(apply_fn)
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT,
        new_yaml=PROPOSED, diff="d", rationale="r")

    result = queue.approve(rid)

    assert result is None
    row = queue.get(rid)
    assert row["status"] == "approved"
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == PROPOSED
