"""CONCISE_REPLIES / SHADOW_CONCISE_REPLIES: a per-account system-prompt
note that makes the chat agent lead with the answer. Off by default; with
both off the prompt is exactly what it was (paper runs a live experiment)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from allpath_trade.agent.context import CONCISE_REPLY_NOTE
from allpath_trade.config import Settings, account_flag
from allpath_trade.web.app import create_app
from tests.helpers import CONFIGURED_KEYS
from tests.test_agent_loop import ScriptedLLM
from tests.test_sentinel import FakeBroker


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_flags_default_off_for_both_accounts():
    s = _settings()
    assert account_flag(s, "concise_replies", "paper") is False
    assert account_flag(s, "concise_replies", "shadow") is False
    assert account_flag(s, "telegram_order_threads", "paper") is False
    assert account_flag(s, "telegram_order_threads", "shadow") is False


def test_shadow_flag_only_turns_on_shadow():
    s = _settings(shadow_concise_replies=True, shadow_telegram_order_threads=True)
    assert account_flag(s, "concise_replies", "shadow") is True
    assert account_flag(s, "concise_replies", "paper") is False
    assert account_flag(s, "telegram_order_threads", "shadow") is True
    assert account_flag(s, "telegram_order_threads", "paper") is False


def test_bare_flag_is_inherited_by_shadow_unless_overridden():
    s = _settings(concise_replies=True)
    assert account_flag(s, "concise_replies", "paper") is True
    assert account_flag(s, "concise_replies", "shadow") is True
    s = _settings(concise_replies=True, shadow_concise_replies=False)
    assert account_flag(s, "concise_replies", "shadow") is False


def test_flags_read_from_env(monkeypatch):
    monkeypatch.setenv("SHADOW_CONCISE_REPLIES", "true")
    monkeypatch.setenv("SHADOW_TELEGRAM_ORDER_THREADS", "true")
    s = _settings()
    assert s.shadow_concise_replies is True and s.shadow_telegram_order_threads is True
    assert s.concise_replies is False and s.telegram_order_threads is False


def _client(tmp_path, monkeypatch, **flags) -> TestClient:
    (tmp_path / "strategies").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        **flags, **CONFIGURED_KEYS)
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    return TestClient(create_app(settings, broker=FakeBroker()))


def _prompt(client, account: str) -> str:
    return client.app.state.chat_services[account].session().system_prompt


def test_shadow_flag_adds_note_to_shadow_prompt_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, shadow_concise_replies=True)
    assert CONCISE_REPLY_NOTE in _prompt(client, "shadow")
    assert CONCISE_REPLY_NOTE not in _prompt(client, "paper")


def test_flags_off_add_nothing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert CONCISE_REPLY_NOTE not in _prompt(client, "shadow")
    assert CONCISE_REPLY_NOTE not in _prompt(client, "paper")


def test_paper_prompt_is_identical_with_shadow_features_on(tmp_path, monkeypatch):
    """The ABAB experiment guard: turning every shadow feature on changes
    nothing in paper's prompt or tool list."""
    off = _client(tmp_path / "off", monkeypatch)
    on = _client(tmp_path / "on", monkeypatch, shadow_concise_replies=True,
                 shadow_telegram_order_threads=True)
    p_off = off.app.state.chat_services["paper"].session()
    p_on = on.app.state.chat_services["paper"].session()
    assert p_on.system_prompt == p_off.system_prompt
    assert ({s.name for s in p_on.registry.specs()}
            == {s.name for s in p_off.registry.specs()})


def test_note_leads_with_the_answer_and_keeps_warnings():
    note = CONCISE_REPLY_NOTE.lower()
    assert "first" in note and "why" in note
    assert "risk" in note  # never bury a block/failed order under brevity
