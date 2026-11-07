"""Shadow check-in pilot: question generation, delivery and idempotency
(spec 2026-11-06-shadow-checkin-design.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from allpath_trade.checkin import (
    CHECKIN_CHAT_NOTE,
    CHECKIN_PROMPT,
    DailyCheckin,
    build_checkin_message,
)
from allpath_trade.llm.base import LLMResponse
from allpath_trade.store.db import connect
from allpath_trade.store.views import CheckinStore, ViewStore
from tests.test_agent_loop import ScriptedLLM
from tests.test_checkin_facts import (
    FIG,
    FIG_DOC,
    FakeBreaker,
    FakeBroker,
    FakeData,
    FakeStrategies,
)

NOW = datetime(2026, 11, 6, 21, 30, tzinfo=UTC)  # 16:30 ET


class FakeAPI:
    def __init__(self, message_id=555):
        self.message_id = message_id
        self.sent: list[tuple[str, str, dict | None]] = []

    def send_message_id(self, chat_id, html, reply_markup=None):
        self.sent.append((chat_id, html, reply_markup))
        return self.message_id


class FakeReflector:
    def briefing(self, et_date):
        return f"# Daily reflection seed briefing -- {et_date}\nNVDA -4.2% today"


class FakeQueue:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def list(self, status="pending"):
        return self.rows


def _checkin(tmp_path, llm, *, api=None, queue=None, broker=None, breaker=None,
             strategies=None):
    conn = connect(tmp_path / "t.db")
    acc = SimpleNamespace(conn=conn, reflector=FakeReflector(),
                          queue=queue or FakeQueue(), account="shadow",
                          broker=broker or FakeBroker(), data=FakeData({"FIG": "18.81"}),
                          sentinel=SimpleNamespace(breaker=breaker),
                          strategies=strategies or FakeStrategies())
    return DailyCheckin(llm=llm, acc=acc, api=api or FakeAPI(), chat_id="111"), conn


def _llm(questions):
    return ScriptedLLM([LLMResponse(text=json.dumps({"questions": questions}))])


def test_sends_questions_with_force_reply_and_records_checkin(tmp_path):
    api = FakeAPI(message_id=555)
    checkin, conn = _checkin(tmp_path, _llm(["NVDA fell 4%: still a buyer under $180?"]),
                             api=api)
    status = checkin.run_daily(now=NOW)
    assert status == "sent 1 question(s)"
    [(chat_id, html, markup)] = api.sent
    assert chat_id == "111"
    assert html.startswith("[Shadow]") and "NVDA fell 4%" in html
    assert markup["force_reply"] is True
    row = CheckinStore(conn, account="shadow").by_message_id(555)
    assert row["et_date"] == "2026-11-06"
    assert row["questions"] == ["NVDA fell 4%: still a buyer under $180?"]


def test_runs_once_per_et_day(tmp_path):
    api = FakeAPI()
    checkin, _ = _checkin(tmp_path, _llm(["Q?"]), api=api)
    checkin.run_daily(now=NOW)
    assert checkin.run_daily(now=NOW) == "already sent (2026-11-06)"
    assert len(api.sent) == 1


def test_caps_at_three_questions_and_drops_blanks(tmp_path):
    api = FakeAPI()
    checkin, _ = _checkin(tmp_path, _llm(["a?", " ", "b?", "c?", "d?"]), api=api)
    assert checkin.run_daily(now=NOW) == "sent 3 question(s)"
    assert "d?" not in api.sent[0][1]


def test_no_questions_means_nothing_sent_and_nothing_recorded(tmp_path):
    api = FakeAPI()
    checkin, conn = _checkin(tmp_path, _llm([]), api=api)
    assert checkin.run_daily(now=NOW) == "nothing to ask"
    assert api.sent == []
    assert not CheckinStore(conn, account="shadow").exists_for("2026-11-06")


def test_unparseable_llm_output_raises(tmp_path):
    checkin, _ = _checkin(tmp_path, ScriptedLLM([LLMResponse(text="no json here")]))
    with pytest.raises(RuntimeError):
        checkin.run_daily(now=NOW)


def test_llm_error_raises(tmp_path):
    class Boom(ScriptedLLM):
        def complete(self, messages, tools=None):
            raise RuntimeError("402 out of credits")

    checkin, _ = _checkin(tmp_path, Boom([]))
    with pytest.raises(RuntimeError):
        checkin.run_daily(now=NOW)


def test_undelivered_message_is_not_recorded(tmp_path):
    checkin, conn = _checkin(tmp_path, _llm(["Q?"]), api=FakeAPI(message_id=None))
    with pytest.raises(RuntimeError):
        checkin.run_daily(now=NOW)
    assert not CheckinStore(conn, account="shadow").exists_for("2026-11-06")


def test_prompt_carries_briefing_pending_items_and_views(tmp_path):
    llm = _llm(["Q?"])
    queue = FakeQueue([{"id": 263, "kind": "strategy_revision", "strategy_id": "NVDA",
                        "ticker": "NVDA", "action": "", "condition": ""}])
    checkin, conn = _checkin(tmp_path, llm, queue=queue)
    ViewStore(conn, account="shadow").add(
        subject="NVDA", stance="long-term bull", horizon="1y", conviction="high",
        invalidation="capex cut", now=NOW)
    checkin.run_daily(now=NOW)
    sent = llm.seen[0][-1]["content"]
    assert "NVDA -4.2% today" in sent
    assert "#263 strategy_revision NVDA" in sent
    assert "long-term bull" in sent
    assert llm.seen[0][0]["content"] == CHECKIN_PROMPT


def test_message_format():
    msg = build_checkin_message("2026-11-06", ["One?", "Two?"])
    assert msg.splitlines()[0] == "[Shadow] Evening check-in (2026-11-06)"
    assert "1. One?" in msg and "2. Two?" in msg
    assert "Reply to this message" in msg


# -- live facts (coordinator 2026-11-07: the first real check-in quoted the
# cost basis as FIG's price, stale CRDO sizing and a breaker already reset) --

def test_live_snapshot_leads_the_facts_and_the_briefing_is_marked_stale(tmp_path):
    llm = _llm(["Q?"])
    checkin, _ = _checkin(tmp_path, llm, broker=FakeBroker([FIG]),
                          breaker=FakeBreaker(tripped=None, peak="300000"),
                          strategies=FakeStrategies([FIG_DOC]))
    checkin.run_daily(now=NOW)
    sent = llm.seen[0][-1]["content"]
    live = sent.index("LIVE ACCOUNT SNAPSHOT")
    stale = sent.index("may be stale")
    assert live < stale < sent.index("NVDA -4.2% today")
    assert "FIG: 100 sh, last $18.81" in sent and "cost basis $35.87/sh" in sent
    assert "Drawdown breaker: NOT tripped" in sent
    assert "FIG $20 call exp 2027-01-15" in sent and "out of the money" in sent


def test_prompt_makes_live_data_win():
    assert "LIVE ACCOUNT SNAPSHOT" in CHECKIN_PROMPT
    assert "cost basis" in CHECKIN_PROMPT and "wins" in CHECKIN_PROMPT


def test_broker_down_raises_and_sends_nothing(tmp_path):
    api = FakeAPI()
    checkin, conn = _checkin(tmp_path, _llm(["Q?"]), api=api,
                             broker=FakeBroker(fail=True))
    with pytest.raises(RuntimeError):
        checkin.run_daily(now=NOW)
    assert api.sent == []
    assert not CheckinStore(conn, account="shadow").exists_for("2026-11-06")


def test_chat_note_pins_down_a_reply_without_a_stance():
    note = CHECKIN_CHAT_NOTE
    assert "no clear stance" in note
    assert "one short yes/no question" in note
    assert "Do not file a view" in note
