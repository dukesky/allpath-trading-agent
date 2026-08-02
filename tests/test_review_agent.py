import json

import pytest

from tests.test_agent_loop import ScriptedLLM, tool_response
from tradewind.agent.review import ReviewAgent
from tradewind.agent.tools import ToolRegistry
from tradewind.llm.base import LLMError, LLMResponse

REVIEW = {"id": 1, "strategy_id": "s", "rule_id": "r1", "ticker": "AAPL",
          "rule_type": "soft", "condition": "price < 205", "action": "buy $3000",
          "snapshot": json.dumps({"price": "204"})}


def registry():
    reg = ToolRegistry()
    reg.register("get_quote", "q", {"type": "object", "properties": {}},
                 lambda **kw: "AAPL: 204")
    return reg


def test_analyze_parses_json_answer():
    llm = ScriptedLLM([
        tool_response("get_quote", {"ticker": "AAPL"}),
        LLMResponse(text='{"recommendation": "execute", "reasoning": "dip", "sources": ["x"]}'),
    ])
    a = ReviewAgent(llm, registry()).analyze(REVIEW)
    assert a.recommendation == "execute" and a.sources == ["x"]


def test_analyze_strips_markdown_fences():
    llm = ScriptedLLM([LLMResponse(
        text='```json\n{"recommendation": "skip", "reasoning": "bad news"}\n```')])
    a = ReviewAgent(llm, registry()).analyze(REVIEW)
    assert a.recommendation == "skip"


def test_unparseable_answer_defaults_to_skip():
    llm = ScriptedLLM([LLMResponse(text="I think maybe buy?")])
    a = ReviewAgent(llm, registry()).analyze(REVIEW)
    assert a.recommendation == "skip" and "unparseable" in a.reasoning


def test_llm_error_propagates():
    with pytest.raises(LLMError):
        ReviewAgent(ScriptedLLM([LLMError("down")]), registry()).analyze(REVIEW)


def test_matching_lessons_uses_word_boundary_not_bare_substring(tmp_path):
    from tradewind.memory.store import MemoryStore
    from tradewind.store.db import connect

    memory = MemoryStore(tmp_path / "memory", connect(tmp_path / "db.sqlite"))
    # "AI" must not match merely because it's a substring of "SAIL".
    memory.apply("lesson", "sailing-note", "add",
                 text="Learned to stay calm and sail steady during volatility")
    memory.apply("lesson", "ai-note", "add", text="AI stocks: don't chase the hype")
    agent = ReviewAgent(ScriptedLLM([]), registry(), memory=memory)
    lessons = agent._matching_lessons("AI")
    assert "hype" in lessons
    assert "sail steady" not in lessons


def test_analyze_prompt_includes_dossier_and_lessons(tmp_path):
    from tradewind.memory.store import MemoryStore
    from tradewind.store.db import connect

    memory = MemoryStore(tmp_path / "memory", connect(tmp_path / "db.sqlite"))
    memory.apply("stock", "AAPL", "add", text="Earnings vol ±8%")
    memory.apply("lesson", "earnings-week", "add",
                 text="AAPL: no new positions in earnings week")
    llm = ScriptedLLM([LLMResponse(
        text='{"recommendation": "skip", "reasoning": "earnings week"}')])
    agent = ReviewAgent(llm, registry(), memory=memory)
    agent.analyze(REVIEW)
    prompt = llm.seen[0][0]["content"]
    assert "Earnings vol" in prompt and "earnings week" in prompt.lower()
