"""Shadow check-in pilot: the chat-side wiring (view tools + prompt note) is
shadow-only and flag-gated (spec 2026-11-06-shadow-checkin-design.md)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.helpers import CONFIGURED_KEYS
from tests.test_agent_loop import ScriptedLLM
from tests.test_sentinel import FakeBroker


def _client(tmp_path, monkeypatch, *, checkin: bool) -> TestClient:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        shadow_daily_checkin=checkin, **CONFIGURED_KEYS)
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    return TestClient(create_app(settings, broker=FakeBroker()))


def _tools(session) -> set[str]:
    return {spec.name for spec in session.registry.specs()}


def test_flag_on_gives_shadow_chat_view_tools_and_note(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, checkin=True)
    shadow = client.app.state.chat_services["shadow"].session()
    assert {"record_view", "list_views"} <= _tools(shadow)
    assert "file every stance he states" in shadow.system_prompt


def test_flag_on_leaves_paper_chat_untouched(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, checkin=True)
    paper = client.app.state.chat_services["paper"].session()
    assert not {"record_view", "list_views"} & _tools(paper)
    assert "file every stance he states" not in paper.system_prompt


def test_flag_off_adds_nothing_to_shadow(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, checkin=False)
    shadow = client.app.state.chat_services["shadow"].session()
    assert not {"record_view", "list_views"} & _tools(shadow)
    assert "file every stance he states" not in shadow.system_prompt
