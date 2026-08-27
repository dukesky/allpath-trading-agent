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
    apply_fn = apply_revision_factory(store)
    apply_fn("s1", PROPOSED, CURRENT)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == PROPOSED
    versions = store.versions("s1")
    assert versions[0]["reason"] == "reflection revision approved via web"
    assert versions[0]["version"] == 2


def test_applier_raises_revision_validation_error_on_invalid_content(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError):
        apply_fn("s1", "name: Bad\nstatus: active\n", CURRENT)  # missing `position`
    # nothing written: the file is untouched
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


def test_applier_wired_through_queue_approve_rolls_back_to_pending(tmp_path):
    _, store, queue = make(tmp_path)
    apply_fn = apply_revision_factory(store)
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
    apply_fn = apply_revision_factory(store)
    queue.set_revision_applier(apply_fn)
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT,
        new_yaml=PROPOSED, diff="d", rationale="r")

    result = queue.approve(rid)

    assert result is None
    row = queue.get(rid)
    assert row["status"] == "approved"
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == PROPOSED


# --- Finding 1 (Critical): staleness must be checked against the file,
# not the already-valid proposal ----------------------------------------

def test_stale_sibling_approval_does_not_revert_an_already_applied_change(tmp_path):
    # Reproduces the reviewer's finding: two proposals drafted from the same
    # base. Approving the first must succeed; approving the second
    # (unaware the file has since moved) must fail re-validation and leave
    # the file exactly as the first approval left it -- not silently revert
    # it back toward the stale base.
    _, store, queue = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    queue.set_revision_applier(apply_fn)

    tightened_stop = PROPOSED  # price < 90, version 2 -- drafted from CURRENT
    rid_a = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT, new_yaml=tightened_stop,
        diff="d", rationale="tighten stop")
    sibling = CURRENT.replace("version: 1", "version: 2").replace(
        "target_weight: 15%", "target_weight: 20%")
    rid_b = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT, new_yaml=sibling,
        diff="d", rationale="unrelated sizing change, same base")

    queue.approve(rid_a)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == tightened_stop

    with pytest.raises(RevisionValidationError):
        queue.approve(rid_b)

    row_b = queue.get(rid_b)
    assert row_b["status"] == "pending"
    # The critical assertion: B's failed approval must not have touched the
    # file A's approval already wrote.
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == tightened_stop


def test_applier_notices_a_notify_email_change_underneath_a_stale_proposal(tmp_path):
    # The exact-text base comparison must catch even a change the parsed
    # StrategyDoc would consider immaterial to the diff being approved
    # (e.g. an unrelated notify_email toggle) -- not just conflicting rule
    # edits.
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    toggled = CURRENT.replace("status: active", "status: active\nnotify_email: true")
    (tmp_path / "strategies" / "s1.yaml").write_text(toggled)

    with pytest.raises(RevisionValidationError):
        apply_fn("s1", PROPOSED, CURRENT)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == toggled


# --- Finding 2: applier pre-flight gates --------------------------------

def test_applier_rejects_path_traversal_strategy_id(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError):
        apply_fn("../escaped", "id: x\n", "")
    assert not (tmp_path / "escaped.yaml").exists()


def test_applier_rejects_a_deleted_strategy_and_does_not_resurrect_it(tmp_path):
    _, store, _ = make(tmp_path)
    (tmp_path / "strategies" / "s1.yaml").unlink()
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError):
        apply_fn("s1", PROPOSED, CURRENT)
    assert not (tmp_path / "strategies" / "s1.yaml").exists()


# --- Finding 3: version must move strictly forward at propose time -----

def test_propose_with_version_not_greater_than_current_is_rejected(tmp_path):
    reg, _, queue = make(tmp_path)
    same_version = PROPOSED.replace("version: 2", "version: 1")
    out = call(reg, strategy_id="s1", new_yaml=same_version, rationale="r")
    assert out.startswith("error:") and "version" in out
    assert queue.list() == []


def test_propose_with_version_omitted_defaults_to_1_is_rejected(tmp_path):
    # Reproduces Finding 3: a proposal that drops `version:` entirely
    # defaults to 1 (StrategyDoc), which would otherwise sort to the bottom
    # of a version-DESC audit trail.
    reg, _, queue = make(tmp_path)
    no_version = PROPOSED.replace("version: 2\n", "")
    out = call(reg, strategy_id="s1", new_yaml=no_version, rationale="r")
    assert out.startswith("error:") and "version" in out
    assert queue.list() == []


def test_propose_against_unparseable_current_file_only_requires_positive_version(tmp_path):
    reg, store, queue = make(tmp_path)
    (store.directory / "s1.yaml").write_text("name: Bad\nstatus: active\n")  # missing position
    out = call(reg, strategy_id="s1", new_yaml=PROPOSED, rationale="repair attempt")
    assert not out.startswith("error:"), out
    assert queue.list()[0]["strategy_id"] == "s1"


# --- Finding 8: reject a no-op proposal ---------------------------------

def test_proposal_identical_to_current_file_is_rejected(tmp_path):
    reg, _, queue = make(tmp_path)
    out = call(reg, strategy_id="s1", new_yaml=CURRENT, rationale="no real change")
    assert out.startswith("error:")
    assert queue.list() == []


# --- Finding F1: authorization/status must not move via a revision -----

def test_propose_flipping_authorization_to_auto_is_rejected(tmp_path):
    reg, _, queue = make(tmp_path)
    auto_flip = PROPOSED.replace("name: \"S1\"", "name: \"S1\"\nauthorization: auto")
    out = call(reg, strategy_id="s1", new_yaml=auto_flip, rationale="tighten and automate")
    assert out.startswith("error:")
    assert "authorization" in out
    assert queue.list() == []


def test_propose_dropping_status_active_is_rejected(tmp_path):
    reg, _, queue = make(tmp_path)
    # PROPOSED sets `status: active` explicitly (same as CURRENT); dropping
    # the line lets StrategyDoc silently default to DRAFT.
    no_status = PROPOSED.replace("status: active\n", "")
    out = call(reg, strategy_id="s1", new_yaml=no_status, rationale="tighten stop")
    assert out.startswith("error:")
    assert "status" in out
    assert queue.list() == []


def test_propose_with_unchanged_authorization_and_status_passes(tmp_path):
    reg, _, queue = make(tmp_path)
    # CURRENT has no `authorization:` line -> defaults to `confirm`. Setting
    # it explicitly to the SAME value the default already resolves to must
    # still pass -- the gate compares resolved values, not raw YAML text.
    explicit_confirm = PROPOSED.replace(
        "name: \"S1\"", "name: \"S1\"\nauthorization: confirm")
    out = call(reg, strategy_id="s1", new_yaml=explicit_confirm, rationale="tighten stop")
    assert not out.startswith("error:"), out
    assert queue.list()[0]["strategy_id"] == "s1"


def test_propose_against_unparseable_current_file_rejects_auto(tmp_path):
    reg, store, queue = make(tmp_path)
    (store.directory / "s1.yaml").write_text("name: Bad\nstatus: active\n")  # missing position
    auto_flip = PROPOSED.replace("name: \"S1\"", "name: \"S1\"\nauthorization: auto")
    out = call(reg, strategy_id="s1", new_yaml=auto_flip, rationale="repair and automate")
    assert out.startswith("error:")
    assert "auto" in out
    assert queue.list() == []


def test_propose_against_unparseable_current_file_requires_explicit_status(tmp_path):
    reg, store, queue = make(tmp_path)
    (store.directory / "s1.yaml").write_text("name: Bad\nstatus: active\n")  # missing position
    no_status = PROPOSED.replace("status: active\n", "")
    out = call(reg, strategy_id="s1", new_yaml=no_status, rationale="repair attempt")
    assert out.startswith("error:")
    assert "status" in out
    assert queue.list() == []


def test_propose_against_unparseable_current_file_with_explicit_non_auto_status_passes(
        tmp_path):
    reg, store, queue = make(tmp_path)
    (store.directory / "s1.yaml").write_text("name: Bad\nstatus: active\n")  # missing position
    out = call(reg, strategy_id="s1", new_yaml=PROPOSED, rationale="repair attempt")
    assert not out.startswith("error:"), out
    assert queue.list()[0]["strategy_id"] == "s1"


def test_applier_rejects_authorization_flip_to_auto(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    auto_flip = PROPOSED.replace("name: \"S1\"", "name: \"S1\"\nauthorization: auto")
    with pytest.raises(RevisionValidationError, match="authorization"):
        apply_fn("s1", auto_flip, CURRENT)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


def test_applier_rejects_status_drop(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    no_status = PROPOSED.replace("status: active\n", "")
    with pytest.raises(RevisionValidationError, match="status"):
        apply_fn("s1", no_status, CURRENT)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


def test_applier_allows_unchanged_authorization_and_status(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    explicit_confirm = PROPOSED.replace(
        "name: \"S1\"", "name: \"S1\"\nauthorization: confirm")
    apply_fn("s1", explicit_confirm, CURRENT)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == explicit_confirm


def test_applier_rejects_authorization_auto_against_unparseable_base(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    unparseable_base = "name: Bad\nstatus: active\n"  # missing position
    # Gate 3 (exact-text match) checks the FILE's current text against
    # `expected_base_yaml` before this check ever runs -- so the on-disk
    # file has to actually be the unparseable text, not just the argument.
    (store.directory / "s1.yaml").write_text(unparseable_base)
    auto_flip = PROPOSED.replace("name: \"S1\"", "name: \"S1\"\nauthorization: auto")
    with pytest.raises(RevisionValidationError, match="auto"):
        apply_fn("s1", auto_flip, unparseable_base)


def test_applier_rejects_missing_status_against_unparseable_base(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    unparseable_base = "name: Bad\nstatus: active\n"  # missing position
    (store.directory / "s1.yaml").write_text(unparseable_base)
    no_status = PROPOSED.replace("status: active\n", "")
    with pytest.raises(RevisionValidationError, match="status"):
        apply_fn("s1", no_status, unparseable_base)


def test_applier_allows_explicit_non_auto_status_against_unparseable_base(tmp_path):
    _, store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    unparseable_base = "name: Bad\nstatus: active\n"  # missing position
    (store.directory / "s1.yaml").write_text(unparseable_base)
    apply_fn("s1", PROPOSED, unparseable_base)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == PROPOSED


# --- Finding F2: an approved revision must not silently re-arm a burned
# trigger state (StrategyStore.rearm_warning / not_armed_rules) -----------

def test_not_armed_rules_reports_rules_whose_state_is_not_armed(tmp_path):
    from allpath_trade.strategy.model import RuleState

    _, store, _ = make(tmp_path)
    store.set_rule_state("s1", "r1", RuleState.TRIGGERED)
    stuck = store.not_armed_rules("s1")
    assert [r.id for r in stuck] == ["r1"]
    assert stuck[0].state == RuleState.TRIGGERED


def test_rearm_warning_is_empty_when_every_rule_is_armed(tmp_path):
    _, store, _ = make(tmp_path)
    assert store.rearm_warning("s1") == ""


def test_rearm_warning_names_the_triggered_rule_and_never_re_arms_it(tmp_path):
    from allpath_trade.strategy.model import RuleState

    _, store, _ = make(tmp_path)
    store.set_rule_state("s1", "r1", RuleState.TRIGGERED)

    warning = store.rearm_warning("s1")

    assert "r1" in warning and "triggered" in warning
    assert "re-arm" in warning
    # The warning is purely informational -- it must never itself flip the
    # rule back to armed.
    assert store.load("s1").rules[0].state == RuleState.TRIGGERED


# --- on_proposed callback hook -----------------------------------------------

def test_on_proposed_receives_the_new_review_id(tmp_path):
    # Arrange: same as test_valid_proposal_queues_and_returns_pending_text
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "s1.yaml").write_text(CURRENT)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(strategies_dir, conn)
    queue = ReviewQueue(conn, executor=None)

    # Set up registry with on_proposed callback
    seen: list[int] = []
    reg = ToolRegistry()
    register_reflection_tools(reg, strategies=store, queue=queue, on_proposed=seen.append)

    # Act
    result = call(reg, strategy_id="s1", new_yaml=PROPOSED, rationale="stop was too loose")

    # Assert
    assert not result.startswith("error:")
    assert len(seen) == 1
    row = queue.get(seen[0])
    assert row["kind"] == "strategy_revision"


def test_on_proposed_not_called_on_a_rejected_proposal(tmp_path):
    # Arrange: same as test_missing_strategy_returns_error_string
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "s1.yaml").write_text(CURRENT)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(strategies_dir, conn)
    queue = ReviewQueue(conn, executor=None)

    # Set up registry with on_proposed callback
    seen: list[int] = []
    reg = ToolRegistry()
    register_reflection_tools(reg, strategies=store, queue=queue, on_proposed=seen.append)

    # Act: call with a missing strategy
    call(reg, strategy_id="missing", new_yaml="x", rationale="r")

    # Assert: callback should not have been called
    assert seen == []
