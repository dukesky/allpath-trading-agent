"""HOLDINGS_SYNC / SHADOW_HOLDINGS_SYNC (allpath_trade/holdings_sync.py):
bring the shadow ledger in line with the owner's real account from a
screenshot extraction, a CSV or a typed list -- never silently, always via
one confirmation. Every holding, price and amount here is invented."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from allpath_trade import holdings_sync as hs
from allpath_trade.agent.shadow_tools import (
    apply_shadow_edit_factory,
    current_shadow_state,
    full_snapshot,
)
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.checkin import DailyCheckin, build_checkin_message
from allpath_trade.config import account_flag
from allpath_trade.llm.base import LLMResponse, ToolCall
from allpath_trade.notify import dispatch
from allpath_trade.research import regime_metrics
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError
from tests.test_agent_loop import ScriptedLLM
from tests.test_card_brief import SpyNotifier, _client, _settings
from tests.test_shadow_ledger import make_ledger
from tests.test_telegram_poller import (
    FakeChatService,
    FakeTelegramAPI,
    make_app_state,
    make_poller,
    pair,
)


def _ledger(tmp_path):
    """A made-up book: AAA 10 @ 50, BBB 5 @ 20, CCC 100 @ 3, cash 1,000."""
    ledger, conn, _data = make_ledger(tmp_path, cash="1000")
    ledger.set_position("AAA", Decimal(10), Decimal(50))
    ledger.set_position("BBB", Decimal(5), Decimal(20))
    ledger.set_position("CCC", Decimal(100), Decimal(3))
    return ledger, conn


class _Strategies:
    def __init__(self, tickers):
        self._tickers = tickers

    def load_all(self, status=None, errors=None):
        return [SimpleNamespace(position=SimpleNamespace(ticker=t)) for t in self._tickers]


def _tool(tmp_path, *, lang="en", planned=("AAA",)):
    ledger, conn = _ledger(tmp_path)
    queue = ReviewQueue(conn, None, account="shadow")
    queue.language = lang
    queue.set_shadow_edit_applier(apply_shadow_edit_factory(ledger, conn))
    registry = ToolRegistry()
    notifier = SpyNotifier()
    hs.register_holdings_sync_tool(
        registry, ledger=ledger, queue=queue, strategies=_Strategies(planned),
        conversation_id_fn=lambda: 7, notifier=notifier)

    def call(**kw):
        return registry.execute(ToolCall(id="x", name="propose_holdings_sync", arguments=kw))

    return call, ledger, conn, queue, notifier


# --- flag -------------------------------------------------------------------

def test_flag_defaults_off_and_is_shadow_only():
    s = _settings()
    assert account_flag(s, hs.FLAG, "shadow") is False
    s = _settings(shadow_holdings_sync=True)
    assert account_flag(s, hs.FLAG, "shadow") is True and account_flag(s, hs.FLAG, "paper") is False
    assert s.shadow_sync_stale_days == 7


def test_tool_and_prompt_note_exist_only_for_shadow_with_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))

    def tools_and_prompt(sub, account, **flags):
        (tmp_path / sub).mkdir()
        client = _client(tmp_path / sub, monkeypatch, **flags)
        session = client.app.state.chat_services[account].session()
        out = ({s.name for s in session.registry.specs()}, session.system_prompt)
        client.__exit__(None, None, None)
        return out

    off_tools, off_prompt = tools_and_prompt("off", "shadow")
    on_tools, on_prompt = tools_and_prompt("on", "shadow", shadow_holdings_sync=True)
    paper_tools, paper_prompt = tools_and_prompt("paper", "paper", shadow_holdings_sync=True)
    paper_off_tools, paper_off_prompt = tools_and_prompt("paper_off", "paper")
    assert "propose_holdings_sync" not in off_tools and "Holdings sync." not in off_prompt
    assert "propose_holdings_sync" in on_tools and hs.HOLDINGS_SYNC_CHAT_NOTE in on_prompt
    assert paper_tools == paper_off_tools and "propose_holdings_sync" not in paper_tools
    assert paper_prompt.replace(str(tmp_path / "paper"), "") == \
        paper_off_prompt.replace(str(tmp_path / "paper_off"), "")


# --- parsing: one output for every input path ---------------------------------

def test_typed_list_csv_and_extraction_give_the_same_rows():
    want = [{"ticker": "AAA", "qty": "40", "avg_cost": "182.5"},
            {"ticker": "BBB", "qty": "2.5", "avg_cost": "1010.25"}]
    typed = hs.parse_holdings_text("aaa 40 @ 182.5, BBB 2.5 shares at $1,010.25, cash 12,000")
    csv_bare = hs.parse_holdings_text("AAA,40,182.5\nBBB,2.5,1010.25\nCASH,12000\n")
    csv_header = hs.parse_holdings_text(
        "Symbol,Name,Quantity,Average Cost\nAAA,Alpha Inc,40,$182.50\n"
        'BBB,Beta Corp,2.5,"1,010.25"\nCash,,12000\n')
    extracted = hs.parse_extraction(
        [{"ticker": "AAA", "qty": "40", "avg_cost": "182.5"},
         {"ticker": "bbb", "qty": 2.5, "avg_cost": "$1,010.25"}], cash="12,000")
    for parsed in (typed, csv_bare, csv_header, extracted):
        assert parsed.problems == []
        assert [(r["ticker"], Decimal(r["qty"]), Decimal(r["avg_cost"]))
                for r in parsed.rows] == [(r["ticker"], Decimal(r["qty"]),
                                           Decimal(r["avg_cost"])) for r in want]
        assert parsed.cash == Decimal(12000)


def test_rows_without_cost_chinese_wording_and_semicolons():
    parsed = hs.parse_holdings_text("AAA 40；BBB 5股 成本 20.5\n现金 300")
    assert parsed.problems == []
    assert parsed.rows == [
        {"ticker": "AAA", "qty": "40", "avg_cost": None, "cost_unknown": False},
        {"ticker": "BBB", "qty": "5", "avg_cost": "20.5", "cost_unknown": False}]
    assert parsed.cash == Decimal(300)


def test_unreadable_and_ambiguous_input_is_listed_not_guessed():
    parsed = hs.parse_holdings_text("AAA 40 @ 50, AAA 10 @ 60, ??? lots, BBB -3, cash abc")
    assert parsed.rows == [{"ticker": "AAA", "qty": "40", "avg_cost": "50",
                            "cost_unknown": False}]
    assert len(parsed.problems) == 4
    assert "AAA appears more than once" in parsed.problems[0]
    assert "could not read" in parsed.problems[1]
    extracted = hs.parse_extraction(
        [{"ticker": "AAA", "qty": "4O"}, {"ticker": "BBB"}, {"ticker": "CCC", "qty": "1",
                                                              "avg_cost": "0"}, "junk"],
        cash="-5", unreadable=["3rd row: quantity cut off"])
    assert extracted.rows == []
    assert len(extracted.problems) == 6
    assert extracted.problems[0] == "unreadable in the image: 3rd row: quantity cut off"
    assert hs.parse_holdings_text("").problems == ["no holdings found in the text"]
    assert hs.parse_extraction(None).problems == ["no holdings were read"]
    for bad in ("NaN", "Infinity", "1e999999"):
        assert hs.parse_holdings_text(f"AAA {bad} @ 5").rows == []


# --- diff -----------------------------------------------------------------

def test_diff_full_sync_new_closed_changed_cash_and_no_plan(tmp_path):
    ledger, _conn = _ledger(tmp_path)
    parsed = hs.parse_holdings_text("AAA 12 @ 55, BBB 5 @ 21, DDD 7 @ 9, cash 800")
    diff = hs.diff_holdings(full_snapshot(ledger), parsed, mode="full", planned={"AAA"})
    assert [e["ticker"] for e in diff.new] == ["DDD"]
    assert [e["ticker"] for e in diff.closed] == ["CCC"]
    assert [(e["ticker"], e["old_qty"], e["qty"]) for e in diff.qty_changed] == [
        ("AAA", "10", "12")]
    assert [e["ticker"] for e in diff.cost_changed] == ["BBB"]
    assert diff.cash_changed and diff.changes == 5
    assert diff.no_plan == ["BBB", "DDD"]
    assert set(diff.after["positions"]) == {"AAA", "BBB", "DDD"} and diff.after["cash"] == "800"
    assert diff.counts() == {"new": 1, "closed": 1, "qty_changed": 1, "cost_changed": 1,
                             "cash_changed": 1, "unchanged": 0, "cost_unknown": 0,
                             "option_lines": 0}


def test_partial_mode_never_closes_and_missing_cost_keeps_or_asks(tmp_path):
    ledger, _conn = _ledger(tmp_path)
    parsed = hs.parse_holdings_text("AAA 12, EEE 3, BBB 5.0 @ 20.00")
    diff = hs.diff_holdings(full_snapshot(ledger), parsed, mode="partial")
    assert diff.closed == [] and diff.not_listed == ["CCC"]
    assert diff.cost_kept == ["AAA"] and diff.qty_changed[0]["avg_cost"] == "50"
    assert diff.needs_cost == ["EEE"] and diff.new == []
    assert diff.unchanged == ["BBB"]          # 5.0 @ 20.00 is 5 @ 20
    assert diff.cash_after is None and not diff.cash_changed
    assert set(diff.after["positions"]) == {"AAA", "BBB", "CCC"}
    # qty 0 in the input closes that symbol explicitly, in either mode.
    zero = hs.diff_holdings(full_snapshot(ledger), hs.parse_holdings_text("CCC 0"),
                            mode="partial")
    assert [e["ticker"] for e in zero.closed] == ["CCC"]


def test_rendered_table_english_and_chinese(tmp_path):
    ledger, _conn = _ledger(tmp_path)
    parsed = hs.parse_holdings_text("AAA 12, BBB 5 @ 21, DDD 7 @ 9.5, cash 800")
    diff = hs.diff_holdings(full_snapshot(ledger), parsed, mode="full", planned={"AAA"})
    assert hs.render_diff(diff, source="screenshot").split("\n") == [
        "Holdings sync (screenshot, whole account)",
        "New (1):", "  DDD  7 sh @ $9.50",
        "Closed, not in the input (1):", "  CCC  was 100 sh @ $3",
        "Quantity changed (1):", "  AAA  10 → 12 sh, cost $50 (kept)",
        "Cost changed (1):", "  BBB  5 sh, cost $20 → $21",
        "Unchanged: 0", "Cash: $1,000 → $800",
        "No plan (held, no active strategy): BBB, DDD"]
    zh = hs.render_diff(diff, source="screenshot", lang="zh").split("\n")
    assert zh[0] == "持仓同步（截图，完整账户）"
    assert zh[1:3] == ["新增（1）：", "  DDD  7 股 @ $9.50"]
    assert "  AAA  10 → 12 股，成本 $50（沿用旧值）" in zh
    assert zh[-2:] == ["现金：$1,000 → $800", "没有计划（有持仓但没有生效的策略）：BBB, DDD"]


# --- the tool: never silent ---------------------------------------------------

def test_problems_and_new_symbols_without_cost_queue_nothing(tmp_path):
    call, ledger, _conn, queue, notifier = _tool(tmp_path)
    before = full_snapshot(ledger)
    out = call(source="screenshot", complete=True,
               positions=[{"ticker": "AAA", "qty": "12", "avg_cost": "55"}],
               unreadable=["row 2: ticker hidden by a banner"])
    assert out.startswith("NOT QUEUED") and "row 2: ticker hidden by a banner" in out
    out = call(source="text", complete=False, text="EEE 3")
    assert out.startswith("NOT QUEUED") and "EEE" in out and "average cost" in out
    assert queue.list() == [] and notifier.sent == [] and full_snapshot(ledger) == before


def test_sync_is_queued_with_table_and_applies_only_on_confirmation(tmp_path, monkeypatch):
    sent = []

    class API:
        def __init__(self, token):
            pass

        def send_message(self, chat_id, html, reply_markup=None):
            sent.append((html, reply_markup))
            return True

    monkeypatch.setattr(dispatch, "TelegramAPI", API)
    ledger, conn = _ledger(tmp_path)
    queue = ReviewQueue(conn, None, account="shadow")
    queue.language = "zh"
    queue.set_shadow_edit_applier(apply_shadow_edit_factory(ledger, conn))
    app_state = AppState(conn)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "42")
    registry = ToolRegistry()
    hs.register_holdings_sync_tool(
        registry, ledger=ledger, queue=queue, strategies=_Strategies(["AAA"]),
        conversation_id_fn=lambda: 7, app_state=app_state, telegram_bot_token="t")
    before = full_snapshot(ledger)
    out = registry.execute(ToolCall(id="x", name="propose_holdings_sync", arguments={
        "source": "screenshot", "complete": True, "cash": "800",
        "positions": [{"ticker": "AAA", "qty": "12"}, {"ticker": "BBB", "qty": "5",
                                                       "avg_cost": "20"},
                      {"ticker": "DDD", "qty": "7", "avg_cost": "9.5"}]}))
    assert out.startswith("QUEUED as #1") and "持仓同步（截图，完整账户）" in out
    assert full_snapshot(ledger) == before, "nothing is applied before confirmation"
    [(html, markup)] = sent
    assert html.startswith("[Shadow] 持仓同步（截图，完整账户）")
    assert "清仓，输入里没有（1）" in html and "你确认之前账本不会变" in html
    assert [b["text"] for b in markup["inline_keyboard"][0]] == ["✅ 批准", "❌ 拒绝"]
    row = queue.get(1)
    assert row["kind"] == "shadow_edit" and row["condition"] == "sync"
    assert row["card_lang"] == "zh" and row["conversation_id"] == 7
    snapshot = json.loads(row["snapshot"])
    assert current_shadow_state(ledger, "sync", snapshot["args"]) == before

    queue.approve(1)
    after = full_snapshot(ledger)
    assert set(after["positions"]) == {"AAA", "BBB", "DDD"}
    assert Decimal(after["positions"]["AAA"]["qty"]) == 12
    assert Decimal(after["positions"]["AAA"]["avg_cost"]) == 50      # cost kept
    assert Decimal(after["cash"]) == 800
    [sync] = [dict(r) for r in conn.execute("SELECT * FROM shadow_syncs")]
    assert sync["source"] == "screenshot" and sync["mode"] == "full" and sync["by"] == "owner"
    assert sync["review_id"] == 1
    assert (sync["n_new"], sync["n_closed"], sync["n_qty_changed"], sync["n_cost_changed"],
            sync["cash_changed"]) == (1, 1, 1, 0, 1)
    assert set(json.loads(sync["before"])["positions"]) == {"AAA", "BBB", "CCC"}
    assert set(json.loads(sync["after"])["positions"]) == {"AAA", "BBB", "DDD"}
    # The audit trail says "adjust", never a buy or a sell: a sync is not a trade.
    sides = {r["side"] for r in conn.execute("SELECT side FROM shadow_orders")}
    assert sides == {"adjust"}
    assert ledger.get_orders(open_only=False) == []


def test_rejecting_or_a_stale_ledger_applies_nothing(tmp_path):
    call, ledger, conn, queue, _notifier = _tool(tmp_path)
    call(source="text", complete=True, text="AAA 12 @ 55, cash 5")
    call(source="text", complete=True, text="AAA 13 @ 55, cash 5")
    before = full_snapshot(ledger)
    queue.reject(1, note="no")
    assert full_snapshot(ledger) == before
    ledger.set_cash(Decimal(999))            # the book moved after the proposal
    with pytest.raises(RevisionValidationError):
        queue.approve(2)
    assert queue.get(2)["status"] == "pending"
    assert conn.execute("SELECT count(*) c FROM shadow_syncs").fetchone()["c"] == 0


def test_a_matching_ledger_still_records_the_reconciliation(tmp_path):
    call, ledger, conn, queue, _notifier = _tool(tmp_path)
    out = call(source="csv", complete=True, text="AAA,10,50\nBBB,5,20\nCCC,100,3\nCASH,1000")
    assert "The ledger already matches" in out and queue.get(1)["action"] == "Holdings sync: no change"
    before = full_snapshot(ledger)
    queue.approve(1)
    assert full_snapshot(ledger) == before
    assert conn.execute("SELECT count(*) c FROM shadow_syncs").fetchone()["c"] == 1
    # Setup wrote 4 audit rows; the sync adds only its "reconciliation:" marker.
    notes = [r["note"] for r in conn.execute("SELECT note FROM shadow_orders ORDER BY id")]
    assert len(notes) == 5 and notes[-1].startswith(
        "reconciliation: holdings sync from csv (full), confirmed by the owner; 0 new")


# --- staleness ------------------------------------------------------------

def test_stale_line_and_checkin_message(tmp_path):
    call, _ledger_, conn, queue, _notifier = _tool(tmp_path)
    now = datetime(2026, 11, 20, 22, tzinfo=UTC)
    assert "never been synced" in hs.stale_line(conn, stale_days=7, now=now)
    call(source="text", complete=False, text="AAA 11")
    queue.approve(1)
    def backdate(days):
        stamp = (now - timedelta(days=days)).isoformat()
        conn.execute("UPDATE shadow_syncs SET ts = ?", (stamp,))
        conn.execute("UPDATE shadow_orders SET ts = ? WHERE note LIKE 'reconciliation:%'",
                     (stamp,))

    backdate(3)
    assert hs.stale_line(conn, stale_days=7, now=now) == ""
    backdate(9)
    line = hs.stale_line(conn, stale_days=7, now=now)
    assert "9 days ago" in line and "screenshot" in line
    assert "9 天前" in hs.stale_line(conn, stale_days=7, now=now, lang="zh")
    assert build_checkin_message("2026-11-20", [], "en", line) == (
        f"[Shadow] Evening check-in (2026-11-20)\n\n{line}")
    assert build_checkin_message("2026-11-20", ["Q?"], "en", line).split("\n")[2:5] == [
        "1. Q?", "", line]


def test_checkin_sends_the_reminder_even_with_no_questions(tmp_path):
    _call, _ledger_, conn, queue, _notifier = _tool(tmp_path)

    class API:
        sent: list = []

        def send_message_id(self, chat_id, html, reply_markup=None):
            API.sent.append(html)
            return 5

    acc = SimpleNamespace(conn=conn, queue=queue, broker=None, data=None, strategies=None,
                          reflector=SimpleNamespace(briefing=lambda d: ""), sentinel=None)
    checkin = DailyCheckin(llm=ScriptedLLM([LLMResponse(text='{"questions": []}')] * 2),
                           acc=acc, api=API(), chat_id="42")
    checkin._facts = lambda et_date, now: "facts"
    assert checkin.run_daily(datetime(2026, 11, 19, 23, tzinfo=UTC)) == "nothing to ask"
    checkin.extra_line = lambda now: hs.stale_line(conn, stale_days=7, now=now)
    assert checkin.run_daily(
        datetime(2026, 11, 20, 23, tzinfo=UTC)) == "sent the holdings-sync reminder"
    assert "never been synced" in API.sent[0]


# --- research marker ------------------------------------------------------

def test_exports_mark_the_first_sync_and_segment_shadow_days(tmp_path):
    call, _ledger_, conn, queue, _notifier = _tool(tmp_path)
    assert regime_metrics.shadow_sync_rows(conn, "2026-11-01", "2026-11-30") == []
    assert regime_metrics.first_shadow_sync_date(conn) is None
    for review_id, text in ((1, "AAA 11"), (2, "AAA 12")):
        call(source="text", complete=False, text=text)
        queue.approve(review_id)
    conn.execute("UPDATE shadow_syncs SET ts = '2026-11-18T15:00:00+00:00' WHERE id = 1")
    conn.execute("UPDATE shadow_syncs SET ts = '2026-11-19T15:00:00+00:00' WHERE id = 2")
    rows = regime_metrics.shadow_sync_rows(conn, "2026-11-01", "2026-11-30")
    assert [(r["date_et"], r["first_sync"], r["n_qty_changed"], r["positions_after"])
            for r in rows] == [("2026-11-18", 1, 1, 3), ("2026-11-19", 0, 1, 3)]
    assert not any("AAA" in str(v) for r in rows for v in r.values())   # counts only
    daily = regime_metrics.daily_rows(conn, "2026-11-17", "2026-11-19")
    segment = {(r["date_et"] if "date_et" in r else r["date"], r["account"]): r["shadow_segment"]
               for r in daily}
    assert segment[("2026-11-17", "shadow")] == "pre_sync"
    assert segment[("2026-11-18", "shadow")] == "synced"
    assert segment[("2026-11-18", "paper")] == ""
    assert regime_metrics.DAILY_COLUMNS[-1] == "shadow_segment"


# --- Telegram: a holdings file --------------------------------------------

def _doc_update(name, file_id="f1", caption=None):
    message = {"message_id": 9, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
               "document": {"file_id": file_id, "file_name": name, "mime_type": "text/csv"}}
    if caption:
        message["caption"] = caption
    return {"update_id": 1, "message": message}


def _poller(tmp_path, flag):
    app_state = make_app_state(tmp_path)
    pair(app_state, 42)
    api = FakeTelegramAPI()
    api.files = {"f1": b"\xef\xbb\xbfAAA,10,50\nCASH,1000\n"}
    chat = FakeChatService()
    poller = make_poller(api, chat, app_state)
    real_get = poller.holder.get

    def get():
        c = real_get()
        c.settings.shadow_holdings_sync = flag
        return c

    poller.holder.get = get
    return poller, api, chat


def test_csv_file_becomes_a_shadow_chat_turn_only_with_the_flag(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    poller, api, chat = _poller(tmp_path / "off", None)
    assert poller._handle_update(_doc_update("holdings.csv")) is None
    assert chat.calls == [] and api.get_file_calls == []
    poller, api, chat = _poller(tmp_path / "on", True)
    poller._handle_update(_doc_update("holdings.csv", caption="sync please"))
    [turn] = chat.calls
    assert turn["text"] == "sync please\n\n[file: holdings.csv]\nAAA,10,50\nCASH,1000\n"
    assert turn["source"] == "telegram" and not turn["images"]
    assert api.download_calls[0][1] == 200_000
    # Other documents are still ignored.
    chat.calls.clear()
    assert poller._handle_update(_doc_update("notes.pdf")) is None and chat.calls == []


# --- stricter reading (independent review findings) ---------------------------

def test_ambiguous_numbers_and_cost_columns_are_asked_about_not_guessed():
    # A decimal comma is not read as a thousands separator.
    parsed = hs.parse_holdings_text("AAA 40 @ 182,5")
    assert parsed.rows == [] and "cannot read the average cost of AAA" in parsed.problems[0]
    # An unmarked third number may be the price, not the cost.
    parsed = hs.parse_holdings_text("AAA 40 190")
    assert parsed.rows == [] and "is 190 the average cost of AAA?" in parsed.problems[0]
    assert hs.parse_holdings_text("AAA 40 @ 190").rows[0]["avg_cost"] == "190"
    # "Cost Basis" in an export is the TOTAL paid: never taken as cost per share.
    for header in ("Symbol,Quantity,Last Price,Cost Basis Total",
                   "Symbol,Quantity,Price,Cost Basis",
                   "Symbol,Quantity,Total Cost"):
        parsed = hs.parse_holdings_text(f"{header}\nAAA,40,182.50,7300\n")
        assert parsed.problems == [] and parsed.rows[0]["avg_cost"] is None, header
    parsed = hs.parse_holdings_text(
        "Symbol,Quantity,Cost Basis Total,Average Cost\nAAA,40,7300,182.50\n")
    assert parsed.rows[0]["avg_cost"] == "182.50"


def test_text_and_positions_together_are_refused(tmp_path):
    call, ledger, _conn, queue, _notifier = _tool(tmp_path)
    out = call(source="text", complete=True, text="AAA 12 @ 55",
               positions=[{"ticker": "BBB", "qty": "5"}])
    assert out.startswith("error: pass EITHER") and queue.list() == []


# --- cost unknown -----------------------------------------------------------

def test_cost_unknown_is_an_explicit_path_not_a_guess(tmp_path):
    ledger, conn = _ledger(tmp_path)
    ledger._data.prices["EEE"] = Decimal("12.50")
    queue = ReviewQueue(conn, None, account="shadow")
    queue.set_shadow_edit_applier(apply_shadow_edit_factory(ledger, conn))
    registry = ToolRegistry()
    hs.register_holdings_sync_tool(registry, ledger=ledger, queue=queue,
                                   strategies=_Strategies([]), conversation_id_fn=lambda: 1,
                                   data=ledger._data)

    def call(**kw):
        return registry.execute(ToolCall(id="x", name="propose_holdings_sync", arguments=kw))

    # No cost and no "unknown": asked, with the way out named.
    out = call(source="text", complete=False, text="EEE 3")
    assert out.startswith("NOT QUEUED") and "cost_unknown" in out and "@ ?" in out
    # Existing AAA: cost unknown keeps the ledger's 50 as a placeholder and flags it.
    # New EEE: placeholder is the price at sync, flagged.
    out = call(source="text", complete=False, text="AAA 10 @ ?, EEE 3 @ unknown")
    assert out.startswith("QUEUED as #1")
    assert "Cost unknown (placeholder cost kept; P/L for these is not real): AAA, EEE" in out
    assert "  EEE  3 sh @ $12.50" in out
    assert ledger.cost_unknown_tickers() == []
    queue.approve(1)
    assert ledger.cost_unknown_tickers() == ["AAA", "EEE"]
    snapshot = full_snapshot(ledger)["positions"]
    assert Decimal(snapshot["AAA"]["avg_cost"]) == 50
    assert Decimal(snapshot["EEE"]["avg_cost"]) == Decimal("12.50")
    assert conn.execute("SELECT n_cost_unknown FROM shadow_syncs").fetchone()[0] == 2
    # A later sync that gives the real cost clears the mark.
    call(source="text", complete=False, text="AAA 10 @ 48")
    queue.approve(2)
    assert ledger.cost_unknown_tickers() == ["EEE"]
    # Without a quote there is nothing honest to use as a placeholder.
    out = call(source="text", complete=False, text="FFF 1 @ ?")
    assert out.startswith("NOT QUEUED") and "FFF" in out


def test_prompt_names_positions_with_unknown_cost(tmp_path):
    from allpath_trade.agent.context import build_system_prompt
    from allpath_trade.store.journal import TradeJournal
    from allpath_trade.strategy.store import StrategyStore

    ledger, conn = _ledger(tmp_path)
    (tmp_path / "s").mkdir()
    kw = {"identity": "id", "broker": ledger, "journal": TradeJournal(conn, account="shadow"),
          "strategies": StrategyStore(tmp_path / "s", conn, account="shadow"),
          "queue": ReviewQueue(conn, None, account="shadow"), "account": "shadow"}
    assert "cost basis UNKNOWN" not in build_system_prompt(**kw)
    ledger.set_cost_unknown("BBB", True)
    assert "cost basis UNKNOWN (placeholder avg; never quote P/L or cost for these): BBB" \
        in build_system_prompt(**kw)


# --- option lines ---------------------------------------------------------

OPT = "ZZZ270115C00150000"


def test_option_lines_from_a_screenshot_and_from_text(tmp_path):
    call, ledger, conn, queue, _notifier = _tool(tmp_path)
    extracted = hs.parse_extraction(
        [], options=[{"underlying": "zzz", "right": "call", "strike": "150",
                      "expiry": "2027-01-15", "contracts": "2", "avg_cost": "12.30",
                      "last_price": "14.10"}])
    typed = hs.parse_holdings_text("OPTION,ZZZ,call,150,2027-01-15,2,12.30,14.10")
    assert extracted.problems == typed.problems == []
    assert extracted.options == typed.options == [
        {"symbol": OPT, "contracts": "2", "avg_cost": "12.30", "last_price": "14.10"}]
    no_cost = hs.parse_extraction([], options=[{"underlying": "ZZZ", "right": "call",
                                                "strike": "150", "expiry": "2027-01-15",
                                                "contracts": "2"}])
    assert no_cost.options == [] and "no cost per share" in no_cost.problems[0]

    before = full_snapshot(ledger)
    out = call(source="screenshot", complete=False, positions=[],
               options=[{"underlying": "ZZZ", "right": "call", "strike": "150",
                         "expiry": "2027-01-15", "contracts": "2", "avg_cost": "12.30",
                         "last_price": "14.10"}])
    assert out.startswith("QUEUED as #1")
    assert "Options:\n  new  ZZZ 2027-01-15 $150 call  2 x @ $12.30" in out
    assert ledger.get_option_rows() == [] and full_snapshot(ledger) == before
    queue.approve(1)
    [row] = ledger.get_option_rows()
    assert (row["symbol"], row["contracts"], row["avg_cost"], row["last_price"]) == (
        OPT, "2", "12.30", "14.10")
    assert conn.execute("SELECT n_option_lines FROM shadow_syncs").fetchone()[0] == 1

    # A new price only; a share-only sync leaves the option line alone.
    out = call(source="text", complete=True,
               text="AAA,10,50\nBBB,5,20\nCCC,100,3\nOPTION,ZZZ,call,150,2027-01-15,2,12.30,16")
    assert "  price  ZZZ 2027-01-15 $150 call  $14.10 → $16" in out
    queue.approve(2)
    assert ledger.get_option_rows()[0]["last_price"] == "16"
    out = call(source="text", complete=True, text="AAA,10,50\nBBB,5,20\nCCC,100,3")
    assert "  not in the input, left as they are: 1" in out
    queue.approve(3)
    assert len(ledger.get_option_rows()) == 1
    # Only an input that claims to show ALL option lines removes a missing one.
    out = call(source="text", complete=False, text="AAA 10 @ 50", options_complete=True)
    assert "  closed  ZZZ 2027-01-15 $150 call  was 2 x @ $12.30" in out
    queue.approve(4)
    assert ledger.get_option_rows() == []


def test_option_book_moving_after_the_proposal_makes_it_stale(tmp_path):
    call, ledger, _conn, queue, _notifier = _tool(tmp_path)
    call(source="text", complete=False, text="OPTION,ZZZ,call,150,2027-01-15,2,12.30")
    ledger.set_option_position(OPT, Decimal(1), Decimal(9))
    with pytest.raises(RevisionValidationError):
        queue.approve(1)
    assert queue.get(1)["status"] == "pending"
    assert ledger.get_option_rows()[0]["contracts"] == "1"


# --- plans and holdings that do not match -------------------------------------

PLAN = """
name: "{ticker} plan"
status: active
authorization: confirm
position: {{ticker: {ticker}, target_weight: 10%}}
rules:
  - {{id: stop, type: hard, condition: "price < 1", action: "sell all", state: {stop}}}
  - {{id: trim, type: hard, condition: "price > 900", action: "sell 25%"}}
  - {{id: entry, type: soft, condition: "price < 2", action: "buy $100"}}
"""


def test_plans_and_holdings_that_do_not_match_are_listed_never_changed(tmp_path):
    from allpath_trade.strategy.store import StrategyStore

    ledger, conn = _ledger(tmp_path)
    directory = tmp_path / "strategies"
    directory.mkdir()
    for ticker, stop in (("AAA", "armed"), ("BBB", "disabled"), ("CCC", "armed"),
                         ("ZZZ", "armed"), ("QQQ", "disabled")):
        (directory / f"{ticker.lower()}-plan.yaml").write_text(
            PLAN.format(ticker=ticker, stop=stop))
    files_before = {p.name: p.read_text() for p in directory.iterdir()}
    store = StrategyStore(directory, conn, account="shadow")
    queue = ReviewQueue(conn, None, account="shadow")
    queue.set_shadow_edit_applier(apply_shadow_edit_factory(ledger, conn))
    registry = ToolRegistry()
    hs.register_holdings_sync_tool(registry, ledger=ledger, queue=queue, strategies=store,
                                   conversation_id_fn=lambda: 1)
    # After this sync: AAA, BBB and DDD are held; CCC is closed; ZZZ is held only
    # as an option; QQQ was never held.
    out = registry.execute(ToolCall(id="x", name="propose_holdings_sync", arguments={
        "source": "text", "complete": True,
        "text": "AAA 10 @ 50, BBB 5 @ 20, DDD 7 @ 9\n"
                "OPTION,ZZZ,call,150,2027-01-15,1,5"}))
    assert "No plan (held, no active strategy): DDD" in out
    assert ("Plans without a position:\n"
            "  ccc-plan (CCC): sell rules still on: stop, trim\n"
            "  qqq-plan (QQQ): sell rules still on: trim") in out
    assert "zzz-plan" not in out.split("Plans without a position:")[1].split("Held,")[0]
    assert "Held, but stops/trims switched off:\n  bbb-plan (BBB): stop" in out
    assert "I will not change or delete a plan on my own." in out
    queue.approve(1)
    assert {p.name: p.read_text() for p in directory.iterdir()} == files_before
    assert [r["kind"] for r in queue.list(status=None)] == ["shadow_edit"]
    assert "switch off the trims and stops" in hs.HOLDINGS_SYNC_CHAT_NOTE


# --- the reconciliation marker --------------------------------------------

def test_sync_writes_a_marker_compatible_with_a_manual_reconciliation(tmp_path):
    call, ledger, conn, queue, _notifier = _tool(tmp_path)
    # A reconciliation done by hand earlier: a marker row, then the adjustments.
    ledger.note_reconciliation("reconciliation: ledger set to brokerage screenshots "
                               "(requested by the owner)")
    ledger.set_position("AAA", Decimal(11), Decimal(50))
    ledger.set_cash(Decimal(900))
    conn.execute("UPDATE shadow_orders SET ts = '2026-11-18T15:00:00+00:00'"
                 " WHERE note LIKE 'reconciliation:%'")
    assert hs.last_sync(conn)["source"] == "manual"
    assert hs.stale_line(conn, stale_days=7,
                         now=datetime(2026, 11, 20, tzinfo=UTC)) == ""
    with pytest.raises(ValueError):
        ledger.note_reconciliation("something else")

    call(source="text", complete=False, text="AAA 12")
    queue.approve(1)
    rows = [dict(r) for r in conn.execute(
        "SELECT ticker, side, status, note FROM shadow_orders ORDER BY id")]
    marker = next(i for i, r in enumerate(rows)
                  if r["note"].startswith("reconciliation: holdings sync"))
    assert (rows[marker]["ticker"], rows[marker]["side"], rows[marker]["status"]) == (
        "", "adjust", "adjusted")
    assert rows[marker + 1]["note"].startswith("set_position AAA")   # adjustments follow it
    conn.execute("UPDATE shadow_syncs SET ts = '2026-11-19T15:00:00+00:00'")
    exported = regime_metrics.shadow_sync_rows(conn, "2026-11-01", "2026-11-30")
    assert [(r["source"], r["first_sync"], r["n_adjusted"]) for r in exported] == [
        ("manual", 1, 2), ("text", 0, 1)]
    assert regime_metrics.first_shadow_sync_date(conn) == "2026-11-18"
    assert not any("AAA" in str(v) or "screenshots" in str(v)
                   for r in exported for v in r.values())
