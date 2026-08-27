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
    apply_fn("new-one", NEW_STRATEGY, "", "chat", is_new=True)
    assert (tmp_path / "strategies" / "new-one.yaml").read_text() == NEW_STRATEGY
    versions = store.versions("new-one")
    assert versions[0]["reason"] == "chat proposal approved via web"
    assert versions[0]["version"] == 1


def test_new_strategy_raises_when_file_already_exists(tmp_path):
    store, _ = make(tmp_path)
    (store.directory / "new-one.yaml").write_text(NEW_STRATEGY)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="created after"):
        apply_fn("new-one", NEW_STRATEGY, "", "chat", is_new=True)


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
        new_yaml=NEW_STRATEGY, diff="d", rationale="r", source="chat",
        is_new=True)

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
    apply_fn("new-two", no_status, "", "chat", is_new=True)
    assert store.load("new-two").status.value == "draft"


def test_new_strategy_negative_version_rejected(tmp_path):
    # Minor: the applier must not trust proposers on the new-strategy path
    # either -- there's no base version to bound it from below, so the
    # applier floors it at a positive integer itself.
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    bad_version = NEW_STRATEGY.replace("version: 1", "version: 0")
    with pytest.raises(RevisionValidationError, match="positive integer"):
        apply_fn("new-three", bad_version, "", "chat", is_new=True)
    assert not (tmp_path / "strategies" / "new-three.yaml").exists()


# --- Important 2: is_new is explicit, not derived from old_yaml=="" ------

def test_empty_existing_file_repair_applies_when_is_new_false(tmp_path):
    # A 0-byte (or otherwise unparseable) EXISTING strategy file also
    # legitimately reads back as old_yaml=="" -- that used to collide with
    # the new-strategy sentinel and make repair proposals against it bounce
    # to pending forever (the applier demanded the file NOT exist, which
    # was always false). is_new=False makes this an ordinary revision: the
    # file must exist and match the recorded (empty) base byte-for-byte --
    # which a genuinely empty file does.
    store, _ = make(tmp_path)
    (store.directory / "s1.yaml").write_text("")
    apply_fn = apply_revision_factory(store)
    apply_fn("s1", PROPOSED, "", "reflection", is_new=False)
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == PROPOSED
    versions = store.versions("s1")
    assert versions[0]["reason"] == "reflection revision approved via web"


def test_empty_existing_file_repair_rejects_if_file_now_missing(tmp_path):
    # Same repair scenario, but the file was deleted between propose and
    # approve -- is_new=False means "must exist", so this must still raise
    # rather than being treated as a new-strategy write.
    store, _ = make(tmp_path)
    (store.directory / "s1.yaml").unlink()
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="no longer exists"):
        apply_fn("s1", PROPOSED, "", "reflection", is_new=False)


def test_is_new_true_still_rejects_when_expected_base_is_not_empty(tmp_path):
    # is_new drives the check now, independent of expected_base_yaml's own
    # content -- confirms the base check isn't secretly still keyed off
    # `expected_base_yaml == ""` under the hood.
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="created after"):
        apply_fn("s1", PROPOSED, CURRENT, "chat", is_new=True)


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


# --- Finding 1a/4: the applier is itself an authoring surface (defense in
# depth -- it must not trust that the propose-time tool already enforced
# these). Exercised via source="chat" (whose authorization/status freeze is
# skipped entirely, see the section above) so CURRENT's default
# authorization: confirm -> auto is allowed through to reach the
# option-authoring checks under test here. -------------------------------

OPTION_NO_EXIT = """\
name: "S1"
status: active
version: 2
authorization: auto
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: entry, type: hard, condition: "price < 100", action: "buy_call $500"}
"""

OPTION_SOFT_RULE = """\
name: "S1"
status: active
version: 2
authorization: auto
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: entry, type: soft, condition: "price < 100", action: "buy_call $500"}
  - {id: exit, type: hard, condition: "price > 999", action: "close_options"}
"""

OPTION_VALID = """\
name: "S1"
status: active
version: 2
authorization: auto
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: entry, type: hard, condition: "price < 100", action: "buy_call $500"}
  - {id: exit, type: hard, condition: "price > 999", action: "close_options"}
"""


def test_applier_rejects_option_action_on_soft_rule(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="type: hard"):
        apply_fn("s1", OPTION_SOFT_RULE, CURRENT, "chat")
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


def test_applier_rejects_option_entry_with_no_exit_rule(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    with pytest.raises(RevisionValidationError, match="close_options"):
        apply_fn("s1", OPTION_NO_EXIT, CURRENT, "chat")
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == CURRENT


def test_applier_accepts_a_valid_option_revision(tmp_path):
    store, _ = make(tmp_path)
    apply_fn = apply_revision_factory(store)
    apply_fn("s1", OPTION_VALID, CURRENT, "chat")
    assert (tmp_path / "strategies" / "s1.yaml").read_text() == OPTION_VALID


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
