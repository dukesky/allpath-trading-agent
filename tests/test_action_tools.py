from decimal import Decimal

from tradewind.agent.action_tools import register_action_tools
from tradewind.agent.tools import ToolRegistry
from tradewind.execution import ExecutionResult
from tradewind.llm.base import ToolCall
from tradewind.risk.gate import RiskDecision
from tradewind.store.db import connect
from tradewind.strategy.store import StrategyStore

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
