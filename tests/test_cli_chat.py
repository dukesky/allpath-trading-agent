from allpath_trade.cli import main
from allpath_trade.llm.base import LLMResponse
from tests.test_agent_loop import ScriptedLLM
from tests.test_sentinel import FakeBroker

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


def test_chat_banner_model_and_tool_activity(tmp_path, capsys, monkeypatch):
    from tests.test_agent_loop import tool_response
    code = run_chat(monkeypatch, tmp_path, ["price?", "/exit"],
                    [tool_response("get_quote", {"ticker": "AAPL"}),
                     LLMResponse(text="around 200")])
    out = capsys.readouterr().out
    assert code == 0
    assert "a l l p a t h" in out          # banner
    assert "scripted" in out                    # model name (ScriptedLLM.model)
    assert "get_quote" in out                   # tool activity line
    assert "around 200" in out                  # rendered reply


def test_chat_registers_memory_tools(tmp_path, capsys, monkeypatch):
    from tests.test_agent_loop import tool_response
    code = run_chat(monkeypatch, tmp_path, ["remember", "/exit"],
                    [tool_response("memory_update",
                                   {"layer": "profile", "action": "add",
                                    "text": "prefers dividends"}),
                     LLMResponse(text="noted")])
    out = capsys.readouterr().out
    assert code == 0 and "noted" in out
    assert (tmp_path / "memory" / "user_profile.md").exists()


def test_chat_eof_runs_same_post_chat_consolidation_as_exit(tmp_path, capsys, monkeypatch):
    from tests.test_agent_loop import tool_response

    setup_env(tmp_path, monkeypatch)

    chat_llm = ScriptedLLM([LLMResponse(text="hi there")])
    memory_llm = ScriptedLLM([
        tool_response("memory_update",
                      {"layer": "profile", "action": "add",
                       "text": "prefers dividends"}),
        LLMResponse(text="noted 1 preference"),
    ])

    def fake_build_llm(settings, tier):
        # consolidation runs on the memory tier, not the sentinel review tier
        return memory_llm if tier == "memory" else chat_llm

    monkeypatch.setattr("allpath_trade.llm.factory.build_llm", fake_build_llm)

    lines = iter(["remember I prefer dividends"])

    def input_then_eof(*a):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", input_then_eof)
    code = main(["chat"], broker_factory=lambda s: FakeBroker(),
                llm_factory=lambda s, tier: chat_llm)
    assert code == 0
    memory_file = tmp_path / "memory" / "user_profile.md"
    assert memory_file.exists()
    assert "dividends" in memory_file.read_text()


def test_chat_finish_skips_consolidation_when_the_setting_is_disabled(
        tmp_path, capsys, monkeypatch):
    # Finding 7: consolidate_after_chat is a real Settings field and a live
    # checkbox on the settings page, but until now nothing read it -- exit
    # consolidated unconditionally regardless of what the user set.
    setup_env(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text('CONSOLIDATE_AFTER_CHAT="false"\n')

    chat_llm = ScriptedLLM([LLMResponse(text="hi there")])
    memory_llm = ScriptedLLM([LLMResponse(text="should never be reached")])

    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier: memory_llm)

    lines = iter(["remember I prefer dividends"])

    def input_then_eof(*a):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", input_then_eof)
    code = main(["chat"], broker_factory=lambda s: FakeBroker(),
                llm_factory=lambda s, tier: chat_llm)
    assert code == 0
    assert not (tmp_path / "memory" / "user_profile.md").exists()
    assert memory_llm.seen == []  # the consolidator's LLM was never touched


def test_chat_double_ctrl_c_exits_gracefully(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    presses = iter([KeyboardInterrupt, KeyboardInterrupt])

    def raising_input(*a):
        raise next(presses)

    monkeypatch.setattr("builtins.input", raising_input)
    code = main(["chat"], broker_factory=lambda s: FakeBroker(),
                llm_factory=lambda s, tier: ScriptedLLM([]))
    out = capsys.readouterr().out
    assert code == 0
    assert "again to exit" in out and "bye" in out


def test_chat_single_ctrl_c_continues(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    events = iter([KeyboardInterrupt, "hello", "/exit"])

    def input_or_raise(*a):
        e = next(events)
        if e is KeyboardInterrupt:
            raise e
        return e

    monkeypatch.setattr("builtins.input", input_or_raise)
    code = main(["chat"], broker_factory=lambda s: FakeBroker(),
                llm_factory=lambda s, tier: ScriptedLLM([LLMResponse(text="hi")]))
    out = capsys.readouterr().out
    assert code == 0
    assert "again to exit" in out and "hi" in out
