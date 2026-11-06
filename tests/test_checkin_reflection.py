"""Shadow check-in pilot: reflection must answer to the owner's views
(spec 2026-11-06-shadow-checkin-design.md)."""

from __future__ import annotations

from allpath_trade.agent.reflection_tools import register_reflection_tools
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import LLMResponse, ToolCall
from allpath_trade.reflect import Reflector, build_briefing
from allpath_trade.store.views import ViewStore
from tests.test_agent_loop import ScriptedLLM
from tests.test_reflect import NOW, REPORT_TEXT, make_components, make_settings

BRIEF = {"et_date": "2026-11-06", "trades": [], "observations": [], "positions": [],
         "pending_counts": {}}
VIEW = {"id": 7, "ts": "2026-11-06T22:00:00+00:00", "subject": "NVDA",
        "stance": "long-term bull, cap adds at 15%", "horizon": "1y",
        "conviction": "high", "invalidation": "capex cut",
        "expires_ts": "2026-12-06T22:00:00+00:00"}


# -- briefing -------------------------------------------------------------

def test_briefing_without_views_is_unchanged():
    assert build_briefing(**BRIEF) == build_briefing(**BRIEF, views=None)
    assert "Owner's active views" not in build_briefing(**BRIEF)


def test_briefing_with_views_lists_them_fenced():
    text = build_briefing(**BRIEF, views=[VIEW])
    block = text.split("## Owner's active views")[1]
    assert "<external-content>" in block
    assert "#7 NVDA" in block and "cap adds at 15%" in block


def test_briefing_with_empty_views_says_so():
    assert "none on file" in build_briefing(**BRIEF, views=[])


# -- propose_strategy_revision alignment gate -----------------------------

def _registry(tmp_path, views_for):
    components = make_components(tmp_path)
    reg = ToolRegistry()
    register_reflection_tools(reg, strategies=components.strategies,
                              queue=components.queue, views_for=views_for)
    return reg, components


def _propose(reg, components, **extra):
    path = components.strategies.directory / "t.yaml"
    new_yaml = path.read_text().replace("version: 1", "version: 2")
    assert new_yaml != path.read_text()
    args = {"strategy_id": "t", "new_yaml": new_yaml, "rationale": "tighten stop"}
    args.update(extra)
    return reg.execute(ToolCall(id="c", name="propose_strategy_revision", arguments=args))


def _schema_props(reg):
    [spec] = [s for s in reg.specs() if s.name == "propose_strategy_revision"]
    return spec.parameters["properties"]


def test_relevant_view_requires_alignment(tmp_path):
    reg, components = _registry(tmp_path, lambda ticker: [VIEW])
    out = _propose(reg, components)
    assert out.startswith("error:") and "view_alignment" in out and "#7" in out
    assert components.queue.list() == []


def test_alignment_must_start_with_a_known_label(tmp_path):
    reg, components = _registry(tmp_path, lambda ticker: [VIEW])
    assert _propose(reg, components, view_alignment="maybe").startswith("error:")


def test_alignment_is_prefixed_to_the_rationale(tmp_path):
    reg, components = _registry(tmp_path, lambda ticker: [VIEW])
    out = _propose(reg, components,
                   view_alignment="contradict: owner wants adds, tape says wait")
    assert "queued" in out
    [row] = components.queue.list()
    assert "View alignment: contradict: owner wants adds" in row["snapshot"]


def test_no_relevant_view_means_alignment_optional(tmp_path):
    reg, components = _registry(tmp_path, lambda ticker: [])
    assert "queued" in _propose(reg, components)


def test_schema_only_offers_alignment_when_views_are_wired(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_views, _ = _registry(tmp_path / "a", lambda ticker: [])
    without, _ = _registry(tmp_path / "b", None)
    assert "view_alignment" in _schema_props(with_views)
    assert "view_alignment" not in _schema_props(without)


# -- Reflector wiring -----------------------------------------------------

def _run(tmp_path, *, account, checkin):
    components = make_components(tmp_path, account=account)
    ViewStore(components.conn, account="shadow").add(
        subject="NVDA", stance="long-term bull", horizon="1y", conviction="high",
        invalidation="capex cut")
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    Reflector(llm=llm, components=components,
              settings=make_settings(shadow_daily_checkin=checkin)).run_daily(now=NOW)
    return llm.seen[0][0]["content"], llm.seen[0][1]["content"]


def test_shadow_with_flag_sees_views_and_the_alignment_rule(tmp_path):
    system, briefing = _run(tmp_path, account="shadow", checkin=True)
    assert "OWNER'S VIEWS" in system
    assert "long-term bull" in briefing


def test_shadow_without_flag_is_unchanged(tmp_path):
    system, briefing = _run(tmp_path, account="shadow", checkin=False)
    assert "OWNER'S VIEWS" not in system
    assert "Owner's active views" not in briefing


def test_paper_never_sees_views(tmp_path):
    system, briefing = _run(tmp_path, account="paper", checkin=True)
    assert "OWNER'S VIEWS" not in system
    assert "Owner's active views" not in briefing


def test_reflector_exposes_the_briefing(tmp_path):
    components = make_components(tmp_path, account="shadow")
    reflector = Reflector(llm=ScriptedLLM([]), components=components,
                          settings=make_settings())
    assert reflector.briefing("2026-11-06").startswith(
        "# Daily reflection seed briefing -- 2026-11-06")
