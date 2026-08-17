"""Tests for allpath_trade.strategy.apply -- the applier now shared by two
strategy_revision proposers (reflection, chat). Reflection's own frozen
behavior (authorization/status freeze, unparseable-base conservative rules)
is covered in tests/test_reflection_tools.py and stays green after the
applier's move out of agent/reflection_tools.py; this file adds the
source-branch coverage: chat's freeze-skip, the new-strategy base check, the
unknown-source reject, and the guards that apply identically to both
sources."""

import pytest

from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError
from allpath_trade.strategy.apply import apply_revision_factory
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

NEW_STRATEGY = """\
name: "New One"
status: draft
version: 1
position: {ticker: NVDA, target_weight: 5%}
rules:
  - {id: r1, type: hard, condition: "price < 50", action: "sell all"}
"""


def make(tmp_path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "s1.yaml").write_text(CURRENT)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(strategies_dir, conn)
    queue = ReviewQueue(conn, executor=None)
    return store, queue


# --- chat: the authorization/status freeze is skipped entirely ----------

def test_chat_authorization_flip_is_applied(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    auto_flip = PROPOSED.replace("name: \"S1\"", "name: \"S1\"\nauthorization: auto")
    apply_fn("s1", auto_flip, CURRENT, "chat")
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == auto_flip


def test_chat_status_change_is_applied(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    deactivated = PROPOSED.replace("status: active", "status: draft")
    apply_fn("s1", deactivated, CURRENT, "chat")
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == deactivated


def test_chat_auto_flip_applies_without_error_warning_is_a_ui_concern(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    auto_flip = PROPOSED.replace("name: \"S1\"", "name: \"S1\"\nauthorization: auto")
    # No exception here: the applier's job is validate-then-write. Any
    # "this switches to auto, are you sure" warning belongs on the
    # approval card / confirm page (spec §①), not this function.
    apply_fn("s1", auto_flip, CURRENT, "chat")
    versions = store.versions("s1")
    assert versions[0]["reason"] == "chat proposal approved via web"


# --- new strategy (spec §②): base check becomes "file must not exist" --

def test_chat_new_strategy_written_when_file_absent(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    apply_fn("new-one", NEW_STRATEGY, "", "chat")
    assert (tmp_path / "strategies" / "new-one.yaml").read_text() == NEW_STRATEGY
    versions = store.versions("new-one")
    assert versions[0]["reason"] == "chat proposal approved via web"
    assert versions[0]["version"] == 1


def test_new_strategy_raises_when_file_already_exists(tmp_path):
    store, _ = make(tmp_path)
    (store.directory / "new-one.yaml").write_text(NEW_STRATEGY)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="created after"):
        apply_fn("new-one", NEW_STRATEGY, "", "chat")


def test_new_strategy_base_empty_but_file_exists_leaves_row_pending_via_queue(tmp_path):
    # End-to-end through ReviewQueue.approve rather than calling the
    # applier directly: someone creates the file after the chat proposal
    # was queued (spec §②'s named scenario), and approving must leave the
    # row pending -- not stuck "approved" with nothing to show for it.
    store, queue = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    queue.set_revision_applier(apply_fn)
    rid = queue.add_strategy_revision(
        strategy_id="new-one", ticker="NVDA", old_yaml="",
        new_yaml=NEW_STRATEGY, diff="d", rationale="r", source="chat")

    (store.directory / "new-one.yaml").write_text(NEW_STRATEGY)

    with pytest.raises(RevisionValidationError):
        queue.approve(rid)

    row = queue.get(rid)
    assert row["status"] == "pending"


def test_chat_new_strategy_missing_status_defaults_to_draft_not_rejected(tmp_path):
    # Guard matrix: do NOT apply reflection's "status must be explicit"
    # rule to chat -- StrategyDoc's draft default is fine here (spec §②:
    # the user activates on the strategy page).
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    no_status = NEW_STRATEGY.replace("status: draft\n", "")
    apply_fn("new-two", no_status, "", "chat")
    assert store.load("new-two").status.value == "draft"


# --- unknown source: fail closed by construction -------------------------

def test_unknown_source_is_rejected(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="unknown proposal source"):
        apply_fn("s1", PROPOSED, CURRENT, "sentinel")
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


# --- guards enforced identically for both sources -------------------------

@pytest.mark.parametrize("source", ["reflection", "chat"])
def test_invalid_strategy_id_rejected_for_both_sources(tmp_path, source):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="invalid strategy id"):
        apply_fn("../escaped", "id: x\n", "", source)
    assert not (tmp_path / "escaped.yaml").exists()


@pytest.mark.parametrize("source", ["reflection", "chat"])
def test_id_change_in_yaml_is_rejected_for_both_sources(tmp_path, source):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    swapped = PROPOSED.replace('name: "S1"', 'id: other-strategy\nname: "S1"')
    with pytest.raises(RevisionValidationError, match="cannot change strategy id"):
        apply_fn("s1", swapped, CURRENT, source)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


@pytest.mark.parametrize("source", ["reflection", "chat"])
def test_version_not_greater_than_current_rejected_for_both_sources(tmp_path, source):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    same_version = PROPOSED.replace("version: 2", "version: 1")
    with pytest.raises(RevisionValidationError, match="version"):
        apply_fn("s1", same_version, CURRENT, source)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


@pytest.mark.parametrize("source", ["reflection", "chat"])
def test_deleted_strategy_rejected_for_both_sources(tmp_path, source):
    store, _ = make(tmp_path)
    (store.directory / "s1.yaml").unlink()
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="no longer exists"):
        apply_fn("s1", PROPOSED, CURRENT, source)
    assert not (tmp_path / "strategies" / "s1.yaml").exists()


@pytest.mark.parametrize("source", ["reflection", "chat"])
def test_stale_base_rejected_for_both_sources(tmp_path, source):
    store, _ = make(tmp_path)
    toggled = CURRENT.replace("status: active", "status: active\nnotify_email: true")
    (store.directory / "s1.yaml").write_text(toggled)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="changed since"):
        apply_fn("s1", PROPOSED, CURRENT, source)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == toggled


@pytest.mark.parametrize("source", ["reflection", "chat"])
def test_invalid_yaml_rejected_for_both_sources(tmp_path, source):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError):
        apply_fn("s1", "name: Bad\nstatus: active\n", CURRENT, source)  # missing position
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


# --- snapshot reason names the source -------------------------------------

def test_reflection_snapshot_reason(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    apply_fn("s1", PROPOSED, CURRENT, "reflection")
    assert store.versions("s1")[0]["reason"] == "reflection revision approved via web"


def test_chat_snapshot_reason(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    apply_fn("s1", PROPOSED, CURRENT, "chat")
    assert store.versions("s1")[0]["reason"] == "chat proposal approved via web"
