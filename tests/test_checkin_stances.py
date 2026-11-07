"""Shadow check-in pilot: the owner's directives get filed as views.

Regression fixture: Tian's first real Telegram round with shadow
(conversation 10, turns 3417-3421, 2026-11-07 00:42-00:46Z). He stated a clear
stance twice -- "Drop the CRDO and FIg action." and "our current strategy is
stay for now" -- both as plain messages, not check-in replies, and the agent
filed nothing, so that night's reflection was not anchored to either.

The scripted tests pin the plumbing and the prompt contract; the
`integration` test replays the same turns against the real chat tier
(`uv run pytest -m integration tests/test_checkin_stances.py`)."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from allpath_trade.checkin import CHECKIN_CHAT_NOTE
from allpath_trade.config import Settings
from allpath_trade.llm.base import LLMResponse
from allpath_trade.store.views import ViewStore
from allpath_trade.web.app import create_app
from tests.helpers import CONFIGURED_KEYS
from tests.test_agent_loop import ScriptedLLM, tool_response
from tests.test_sentinel import FakeBroker

# -- the exact turns (assistant 3417 abridged to its decision-relevant part) --
TURN_3417_ASSISTANT = (
    "Checked all three against live data. Two of the premises in the questions "
    "are off, and one is inverted.\n\n"
    "- CRDO is not ~$10k / ~4%. At $120.51 it's $6,950 = 2.66% of the book. "
    "Your average cost is $167.02, so you're -28% on it.\n"
    "- FIG is $18.81, not ~$35. The ~$35 figure is your share cost basis "
    "($35.87), not spot. So the 8x $20 Jan-2027 calls are out of the money.\n\n"
    "1. CRDO -- effectively capped; don't add full-size deeper rungs. Rewriting "
    "it means fewer, wider rungs.\n"
    "3. FIG -- trim the calls: sell 4 of the 8 contracts to cut theta.")
TURN_3418_USER = "Drop the CRDO and FIg action."
TURN_3420_USER = (
    "Ok, sounds good, our current strategy is stay for now. I I didn’t apply "
    "your strategy. What do you think of the current result? What if I apply "
    "your strategy? Are we going to have better performance for now?")
STANCELESS_USER = "Ok, sounds good."


def _client(tmp_path, monkeypatch, llm) -> TestClient:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        shadow_daily_checkin=True, **CONFIGURED_KEYS)
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: llm)
    return TestClient(create_app(settings, broker=FakeBroker()))


def _seed(chat) -> None:
    session = chat.session()
    session._append({"role": "user", "content": "[Check-in reply (2026-11-06)]\n"
                     "Questions asked:\n1. CRDO ladder ...\n\nMy answer:\nReply here",
                     "source": "telegram"})
    session._append({"role": "assistant", "content": TURN_3417_ASSISTANT})


def test_directives_in_plain_messages_are_filed_and_confirmed(tmp_path, monkeypatch):
    llm = ScriptedLLM([
        # turn 3418: two subjects, only subject + stance given
        tool_response("record_view", {"subject": "CRDO",
                      "stance": "Hold: keep the ladder as it is; no rewrite"}, id_="v1"),
        tool_response("record_view", {"subject": "FIG",
                      "stance": "No action on the 8x $20C Jan-2027 calls; hold"}, id_="v2"),
        LLMResponse(text="Filed as view #1: CRDO hold, ladder unchanged, 30 days.\n"
                         "Filed as view #2: FIG no option action, 30 days."),
        # turn 3420: book-wide "stay for now" -> MARKET
        tool_response("record_view", {"subject": "MARKET",
                      "stance": "Stay with the current strategy for now; no changes"},
                      id_="v3"),
        LLMResponse(text="Filed as view #3: MARKET stay with the current strategy, "
                         "30 days. On performance: ..."),
        # a genuinely stance-less reply files nothing
        LLMResponse(text="Which one: keep CRDO's ladder as is -- yes/no?"),
    ])
    client = _client(tmp_path, monkeypatch, llm)
    chat = client.app.state.chat_services["shadow"]
    _seed(chat)

    reply = chat.send(TURN_3418_USER, source="telegram")
    assert "Filed as view #1: CRDO" in reply
    reply = chat.send(TURN_3420_USER, source="telegram")
    assert "Filed as view #3: MARKET" in reply
    chat.send(STANCELESS_USER, source="telegram")

    conn = client.app.state.holder.get().conn
    views = ViewStore(conn, "shadow").active()
    assert [v["subject"] for v in views] == ["CRDO", "FIG", "MARKET"]
    assert all(v["horizon"] == "not stated" and v["invalidation"] == "not stated"
               for v in views)
    # MARKET binds every ticker, so tonight's CRDO/FIG revisions must answer to it
    assert {v["subject"] for v in ViewStore(conn, "shadow").relevant("CRDO")} == {
        "CRDO", "MARKET"}
    # the tool told the agent exactly what to confirm
    tool_results = [m["content"] for m in chat.session().history if m["role"] == "tool"]
    assert tool_results[0].startswith("Filed as view #1: CRDO -- Hold: keep the ladder")
    assert "30 days" in tool_results[0]


def test_note_makes_directives_in_any_message_count():
    note = CHECKIN_CHAT_NOTE
    assert "plain message" in note
    for phrase in ("keep", "drop the action", "hold", "cap", "exit"):
        assert phrase in note
    assert '"Hold, no change" is a stance' in note
    # the fixture's own words are the worked examples
    assert TURN_3418_USER in note and "stay for now" in note
    assert "Filed as view #N:" in note
    assert "Do not file a view" in note  # still nothing for stance-less replies


def test_note_no_longer_calls_sounds_good_with_a_stance_stanceless():
    # "sounds good" alone is stance-less; "sounds good, ... stay for now" is not
    assert '"sounds good" on its own' in CHECKIN_CHAT_NOTE


# -- live replay against the real chat tier ----------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("OPENROUTER_API_KEY") and not os.path.exists(".env"),
                    reason="no chat-tier credentials")
def test_live_chat_tier_files_the_fixture_directives(tmp_path):
    base = Settings()
    if not (base.openrouter_api_key or base.openai_api_key):
        pytest.skip("no chat-tier credentials")
    (tmp_path / "strategies").mkdir()
    settings = base.model_copy(update={
        "db_path": tmp_path / "t.db", "strategies_dir": tmp_path / "strategies",
        "memory_dir": tmp_path / "memory", "shadow_daily_checkin": True,
        "telegram_bot_token": ""})
    client = TestClient(create_app(settings, broker=FakeBroker()))
    c = client.app.state.holder.get()
    ledger = c.accounts["shadow"].broker
    ledger.set_cash(Decimal(43606))
    ledger.set_position("CRDO", Decimal("57.67"), Decimal("167.02"))
    ledger.set_position("FIG", Decimal(100), Decimal("35.87"))
    chat = client.app.state.chat_services["shadow"]
    _seed(chat)

    print("\n>>> 3418:", chat.send(TURN_3418_USER, source="telegram"))
    after_3418 = {v["subject"] for v in ViewStore(c.conn, "shadow").active()}
    assert {"CRDO", "FIG"} <= after_3418, after_3418
    print("\n>>> 3420:", chat.send(TURN_3420_USER, source="telegram"))
    after_3420 = ViewStore(c.conn, "shadow").active()
    for v in after_3420:
        print("view:", v["id"], v["subject"], "|", v["stance"])
    assert len(after_3420) > len(after_3418), [v["subject"] for v in after_3420]
