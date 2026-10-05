import json

import pytest

from allpath_trade.agent.review import ReviewAgent
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import LLMError, LLMResponse
from tests.test_agent_loop import ScriptedLLM, tool_response

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


# Real failure mode (found 2026-09-14): the review model ignores "answer ONLY
# with JSON" and wraps the fenced JSON in prose. The old parser only stripped
# a fence around the ENTIRE text, so every such reply became "skip —
# unparseable" and the approval cards showed the wrong recommendation.
def _analyze_text(text):
    return ReviewAgent(ScriptedLLM([LLMResponse(text=text)]), registry()).analyze(REVIEW)


def test_fenced_json_inside_prose_is_parsed():
    a = _analyze_text(
        "Looking at the setup, the dip is orderly.\n\n```json\n"
        '{"recommendation": "execute", "reasoning": "orderly dip", "sources": ["s1"]}\n'
        "```\n\nLet me know if you want more detail.")
    assert a.recommendation == "execute"
    assert a.reasoning == "orderly dip" and a.sources == ["s1"]


def test_bare_json_after_prose_is_parsed():
    a = _analyze_text('Here is my answer: {"recommendation": "skip", "reasoning": "gap risk"}')
    assert a.recommendation == "skip" and a.reasoning == "gap risk"


def test_last_fenced_answer_wins():
    a = _analyze_text(
        'Format example:\n```json\n{"recommendation": "skip", "reasoning": "example"}\n```\n'
        'Final:\n```json\n{"recommendation": "execute", "reasoning": "final"}\n```')
    assert a.recommendation == "execute" and a.reasoning == "final"


def test_braces_inside_reasoning_strings_do_not_break_parsing():
    a = _analyze_text(
        'Answer below.\n```json\n{"recommendation": "execute", '
        '"reasoning": "condition {price < 205} held"}\n```')
    assert a.recommendation == "execute"
    assert a.reasoning == "condition {price < 205} held"


def test_json_with_an_invalid_recommendation_still_reads_unparseable():
    a = _analyze_text('```json\n{"recommendation": "hold", "reasoning": "x"}\n```')
    assert a.recommendation == "skip" and "unparseable" in a.reasoning


def test_llm_error_propagates():
    with pytest.raises(LLMError):
        ReviewAgent(ScriptedLLM([LLMError("down")]), registry()).analyze(REVIEW)


def test_matching_lessons_uses_word_boundary_not_bare_substring(tmp_path):
    from allpath_trade.memory.store import MemoryStore
    from allpath_trade.store.db import connect

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
    from allpath_trade.memory.store import MemoryStore
    from allpath_trade.store.db import connect

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
