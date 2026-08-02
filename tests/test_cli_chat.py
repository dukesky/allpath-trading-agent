from tests.test_agent_loop import ScriptedLLM
from tests.test_sentinel import FakeBroker
from tradewind.cli import main
from tradewind.llm.base import LLMResponse

STRAT = """
name: "T"
status: active
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


def setup_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "t.yaml").write_text(STRAT)


def run_chat(monkeypatch, tmp_path, user_lines, llm_responses):
    setup_env(tmp_path, monkeypatch)
    lines = iter(user_lines)
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    return main(["chat"],
                broker_factory=lambda s: FakeBroker(),
                llm_factory=lambda s, tier: ScriptedLLM(llm_responses))


def test_chat_round_trip_and_exit(tmp_path, capsys, monkeypatch):
    code = run_chat(monkeypatch, tmp_path, ["hello", "/exit"],
                    [LLMResponse(text="hi there")])
    out = capsys.readouterr().out
    assert code == 0
    assert "hi there" in out


def test_chat_eof_exits_cleanly(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)

    def raise_eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    code = main(["chat"], broker_factory=lambda s: FakeBroker(),
                llm_factory=lambda s, tier: ScriptedLLM([]))
    assert code == 0


def test_chat_without_llm_config_exits_2(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code = main(["chat"], broker_factory=lambda s: FakeBroker())
    assert code == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_chat_resumes_latest_conversation(tmp_path, capsys, monkeypatch):
    run_chat(monkeypatch, tmp_path, ["hello", "/exit"], [LLMResponse(text="one")])
    # second run resumes; ScriptedLLM sees prior history in its messages
    llm = ScriptedLLM([LLMResponse(text="two")])
    lines = iter(["again", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    main(["chat"], broker_factory=lambda s: FakeBroker(),
         llm_factory=lambda s, tier: llm)
    assert any(m.get("content") == "hello" for m in llm.seen[0])
