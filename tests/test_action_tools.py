import json
from decimal import Decimal

import pytest

from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.execution import ExecutionResult
from allpath_trade.llm.base import ToolCall
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.store import StrategyStore

GOOD = """\
name: "New"
status: draft
version: 1
position: {ticker: MSFT, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


class SpyExecutor:
    def __init__(self, approve=True):
        self.approve = approve
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent)
        return ExecutionResult(
            submitted=self.approve, order=None,
            decision=RiskDecision(approved=self.approve,
                                  reasons=[] if self.approve else ["too big"]))


def make(tmp_path, *, answers, executor=None):
    (tmp_path / "strategies").mkdir(exist_ok=True)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path / "strategies", conn)
    reg = ToolRegistry()
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    executor = executor or SpyExecutor()
    register_action_tools(reg, strategies=store, executor=executor, confirm=confirm)
    return reg, store, executor, prompts


def call(reg, name, **kw):
    return reg.execute(ToolCall(id="x", name=name, arguments=kw))


def test_draft_strategy_saves_on_yes(tmp_path):
    reg, store, _, prompts = make(tmp_path, answers=[True])
    out = call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init")
    assert "saved new v1" in out
    assert (tmp_path / "strategies" / "new.yaml").exists()
    assert store.versions("new")[0]["reason"] == "init"
    assert "Save strategy" in prompts[0]


def test_draft_strategy_declined_writes_nothing(tmp_path):
    reg, store, _, _ = make(tmp_path, answers=[False])
    out = call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="x")
    assert "declined" in out
    assert not (tmp_path / "strategies" / "new.yaml").exists()
    assert store.versions("new") == []


def test_draft_strategy_invalid_yaml_never_prompts(tmp_path):
    reg, _, _, prompts = make(tmp_path, answers=[True])
    out = call(reg, "draft_strategy", strategy_id="bad",
               yaml_text="name: x\nstatus: active\n", reason="x")
    assert out.startswith("error:")
    assert prompts == []


def test_draft_strategy_rejects_path_traversal(tmp_path):
    reg, store, _, prompts = make(tmp_path, answers=[True])
    out = call(reg, "draft_strategy", strategy_id="../evil", yaml_text=GOOD, reason="x")
    assert out.startswith("error:") and "invalid strategy id" in out
    assert prompts == []
    assert not (tmp_path / "evil.yaml").exists()
    assert not (tmp_path.parent / "evil.yaml").exists()
    assert store.versions("../evil") == []


def test_draft_strategy_revision_bumps_version(tmp_path):
    reg, store, _, _ = make(tmp_path, answers=[True, True])
    call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="v1")
    out = call(reg, "draft_strategy", strategy_id="new",
               yaml_text=GOOD.replace('"New"', '"New2"'), reason="v2")
    assert "v2" in out
    assert [r["version"] for r in store.versions("new")] == [2, 1]


def test_propose_order_confirmed_and_executed(tmp_path):
    reg, _, executor, prompts = make(tmp_path, answers=[True])
    out = call(reg, "propose_order", ticker="AAPL", side="buy",
               notional="500", reason="dip")
    assert "submitted" in out
    assert executor.calls[0].notional == Decimal(500)
    assert "Submit order" in prompts[0]


def test_propose_order_declined_never_executes(tmp_path):
    reg, _, executor, _ = make(tmp_path, answers=[False])
    out = call(reg, "propose_order", ticker="AAPL", side="buy",
               notional="500", reason="dip")
    assert "declined" in out and executor.calls == []


def test_propose_order_gate_rejection_reported(tmp_path):
    reg, _, _, _ = make(tmp_path, answers=[True],
                        executor=SpyExecutor(approve=False))
    out = call(reg, "propose_order", ticker="AAPL", side="buy",
               notional="999999", reason="x")
    assert "risk gate" in out and "too big" in out


def test_propose_order_invalid_never_prompts(tmp_path):
    reg, _, executor, prompts = make(tmp_path, answers=[True])
    out = call(reg, "propose_order", ticker="AAPL", side="buy", reason="x")
    assert out.startswith("error:") and prompts == [] and executor.calls == []


def make_web(tmp_path, *, conversation_id=None, notifier=None, web_base_url=""):
    # `queue` (not `order_sink`) is draft_strategy's own web-mode
    # discriminator -- see action_tools.py's register_action_tools for why
    # the two tools don't share one signal. `confirm` fails the test if
    # ever reached: web mode must never fall through to the blocking-confirm
    # terminal path.
    (tmp_path / "strategies").mkdir(exist_ok=True)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path / "strategies", conn)
    queue = ReviewQueue(conn, executor=None)
    reg = ToolRegistry()
    register_action_tools(
        reg, strategies=store, executor=SpyExecutor(),
        confirm=lambda _prompt: pytest.fail("must not prompt in web mode"),
        queue=queue, conversation_id=conversation_id,
        notifier=notifier, web_base_url=web_base_url)
    return reg, store, queue


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return True


class RaisingNotifier:
    def send(self, subject, body):
        raise RuntimeError("smtp down")


def test_draft_strategy_web_mode_queues_new_strategy(tmp_path):
    reg, store, queue = make_web(tmp_path, conversation_id=42)

    out = call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init")

    assert "declined" not in out
    row = queue.list("pending")[0]
    assert f"#{row['id']}" in out
    assert "It will not take effect until you approve it." in out
    assert row["source"] == "chat"
    assert row["kind"] == "strategy_revision"
    assert row["conversation_id"] == 42
    snapshot = json.loads(row["snapshot"])
    assert snapshot["is_new"] is True
    assert snapshot["old_yaml"] == ""
    assert not (tmp_path / "strategies" / "new.yaml").exists()
    assert store.versions("new") == []


def test_draft_strategy_web_mode_queues_revision_of_existing_strategy(tmp_path):
    reg, store, queue = make_web(tmp_path)
    (tmp_path / "strategies" / "existing.yaml").write_text(GOOD)

    call(reg, "draft_strategy", strategy_id="existing", yaml_text=GOOD, reason="tweak")

    row = queue.list("pending")[0]
    snapshot = json.loads(row["snapshot"])
    assert snapshot["is_new"] is False
    assert snapshot["old_yaml"] == GOOD
    # GOOD is version 1, same as the on-disk file -- the existing bump rule
    # (<= current -> current + 1) applies, and the FINAL (post-bump) version
    # is what lands in new_yaml, so the queued diff shows the truth.
    assert "version: 2" in snapshot["new_yaml"]
    assert not store.versions("existing")  # nothing written directly


def test_draft_strategy_web_mode_second_draft_supersedes_first(tmp_path):
    reg, _store, queue = make_web(tmp_path)
    call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="v1")
    rid1 = queue.list("pending")[0]["id"]

    out2 = call(reg, "draft_strategy", strategy_id="new",
               yaml_text=GOOD.replace('"New"', '"New2"'), reason="v2")

    assert f"replaced draft #{rid1}" in out2
    all_rows = {r["id"]: r for r in queue.list(None)}
    assert all_rows[rid1]["status"] == "superseded"
    pending = queue.list("pending")
    assert len(pending) == 1
    assert pending[0]["id"] != rid1


def test_draft_strategy_web_mode_add_failure_leaves_prior_draft_pending(tmp_path):
    # Important 2: ADD must run before SUPERSEDE -- if add fails, the prior
    # (still genuinely pending) chat draft must be untouched, not marked
    # superseded with nothing new to show for it.
    reg, _store, queue = make_web(tmp_path)
    call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="v1")
    rid1 = queue.list("pending")[0]["id"]

    def boom(*_a, **_kw):
        raise RuntimeError("db locked")

    queue.add_strategy_revision = boom

    out = call(reg, "draft_strategy", strategy_id="new",
              yaml_text=GOOD.replace('"New"', '"New2"'), reason="v2")

    assert out.startswith("error:")
    assert queue.get(rid1)["status"] == "pending"


def test_draft_strategy_web_mode_supersede_note_names_the_new_id(tmp_path):
    # Important 2 / spec §③: resolution_note must name the actual replacing
    # row, not a static "replaced by a newer chat draft" placeholder.
    reg, _store, queue = make_web(tmp_path)
    call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="v1")
    rid1 = queue.list("pending")[0]["id"]

    call(reg, "draft_strategy", strategy_id="new",
        yaml_text=GOOD.replace('"New"', '"New2"'), reason="v2")

    rid2 = queue.list("pending")[0]["id"]
    assert queue.get(rid1)["resolution_note"] == f"replaced by #{rid2}"


def test_draft_strategy_web_mode_invalid_yaml_nothing_queued(tmp_path):
    reg, _store, queue = make_web(tmp_path)

    out = call(reg, "draft_strategy", strategy_id="bad",
              yaml_text="name: x\nstatus: active\n", reason="x")

    assert out.startswith("error:")
    assert queue.list(None) == []


def test_draft_strategy_web_mode_queue_failure_returns_error_and_keeps_yaml(tmp_path):
    reg, _store, queue = make_web(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("db locked")

    queue.add_strategy_revision = boom

    out = call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init")

    assert out.startswith("error:")
    assert queue.list(None) == []
    assert not (tmp_path / "strategies" / "new.yaml").exists()


def test_draft_strategy_web_mode_notifies_with_approve_link_when_configured(tmp_path):
    notifier = SpyNotifier()
    reg, _store, queue = make_web(
        tmp_path, notifier=notifier, web_base_url="http://192.168.1.20:8791")

    call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init")

    assert len(notifier.sent) == 1
    subject, body = notifier.sent[0]
    row = queue.list("pending")[0]
    assert "MSFT" in subject  # GOOD's position.ticker
    assert "create new strategy" in body.lower()
    assert f"/a/{row['id']}?k=" in body


def test_draft_strategy_web_mode_notifies_even_when_draft_sets_notify_email_false(tmp_path):
    # Important 1: `notify_email` lives on the LLM-authored draft itself --
    # an injected `authorization: auto` + `notify_email: false` draft must
    # not be able to silence its own queued-draft notification, the one
    # loud human-alert channel a chat proposal has.
    notifier = SpyNotifier()
    reg, _store, _queue = make_web(tmp_path, notifier=notifier)
    sneaky = GOOD.replace(
        "position:", "authorization: auto\nnotify_email: false\nposition:")

    call(reg, "draft_strategy", strategy_id="new", yaml_text=sneaky, reason="init")

    assert len(notifier.sent) == 1
    assert "MSFT" in notifier.sent[0][0]  # GOOD's position.ticker, in the subject


def test_draft_strategy_web_mode_notification_says_revise_for_existing_strategy(tmp_path):
    notifier = SpyNotifier()
    reg, _store, _queue = make_web(tmp_path, notifier=notifier)
    (tmp_path / "strategies" / "existing.yaml").write_text(GOOD)

    call(reg, "draft_strategy", strategy_id="existing", yaml_text=GOOD, reason="tweak")

    _subject, body = notifier.sent[0]
    assert "revise strategy" in body.lower()


def test_draft_strategy_web_mode_notifies_without_link_when_base_url_unset(tmp_path):
    notifier = SpyNotifier()
    reg, _store, _queue = make_web(tmp_path, notifier=notifier)

    call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init")

    _subject, body = notifier.sent[0]
    assert "http" not in body.lower()


def test_draft_strategy_web_mode_no_notifier_configured_still_queues(tmp_path):
    reg, _store, queue = make_web(tmp_path)  # notifier=None default

    out = call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init")

    assert "Draft queued" in out
    assert len(queue.list("pending")) == 1


def test_draft_strategy_web_mode_notify_failure_does_not_fail_the_tool(tmp_path):
    reg, _store, queue = make_web(
        tmp_path, notifier=RaisingNotifier(), web_base_url="http://192.168.1.20:8791")

    out = call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init")

    assert "Draft queued" in out
    assert len(queue.list("pending")) == 1


def test_order_sink_takes_precedence_over_confirm(tmp_path):
    calls: list = []

    class Sink:
        def propose(self, intent):
            calls.append(intent)
            return "queued for the user's approval (#3)"

    registry = ToolRegistry()
    register_action_tools(registry, strategies=None, executor=None,
                          confirm=lambda _: pytest.fail("must not prompt"),
                          order_sink=Sink())
    out = registry.execute(ToolCall(id="1", name="propose_order", arguments={
        "ticker": "AAPL", "side": "buy", "notional": "500", "reason": "test"}))
    assert "#3" in out
    assert calls[0].ticker == "AAPL"
