"""OPTION_HOLDINGS / SHADOW_OPTION_HOLDINGS (allpath_trade/shadow_options.py):
the shadow ledger can hold and mark long options, entered through the
confirmed-edit path. It never trades them and nothing can propose an option
order from them. Every ticker, strike, price and amount here is invented."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from allpath_trade import shadow_options as so
from allpath_trade.agent.shadow_tools import (
    apply_shadow_edit_factory,
    current_shadow_state,
    full_snapshot,
    option_snapshot,
    parse_shadow_csv,
    preview_import,
    register_shadow_option_tools,
)
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.checkin_facts import live_snapshot
from allpath_trade.config import account_flag
from allpath_trade.llm.base import ToolCall
from allpath_trade.sentinel import Sentinel
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError
from allpath_trade.strategy.store import StrategyStore
from tests.test_agent_loop import ScriptedLLM
from tests.test_card_brief import SpyNotifier, _client, _settings
from tests.test_sentinel import SpyExecutor
from tests.test_shadow_ledger import make_ledger

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
CALL = "ZZZ270115C00150000"          # ZZZ 2027-01-15 $150 call
C = {"underlying": "ZZZ", "right": "call", "strike": "150", "expiry": "2027-01-15"}

STRATEGY = """
name: "ZZZ calls review reminders"
status: active
authorization: {auth}
thesis: "Hold the calls while the trend holds."
position: {{ticker: ZZZ, target_weight: 10%}}
rules:
  - {{id: r1, type: hard, condition: "price < 250", action: "{action}"}}
"""


def _world(tmp_path, *, price="140", cash="10000"):
    ledger, conn, data = make_ledger(tmp_path, prices={"ZZZ": Decimal(price)}, cash=cash)
    queue = ReviewQueue(conn, None, account="shadow")
    queue.set_shadow_edit_applier(apply_shadow_edit_factory(ledger, conn))
    return ledger, conn, data, queue


def _tools(tmp_path, **kw):
    ledger, conn, data, queue = _world(tmp_path, **kw)
    (tmp_path / "strategies").mkdir(exist_ok=True)
    store = StrategyStore(tmp_path / "strategies", conn, account="shadow")
    registry = ToolRegistry()
    notifier = SpyNotifier()
    register_shadow_option_tools(registry, ledger=ledger, queue=queue, data=data,
                                 strategies=store, conversation_id_fn=lambda: 4,
                                 notifier=notifier)

    def call(name, **args):
        return registry.execute(ToolCall(id="x", name=name, arguments=args))

    return call, ledger, conn, queue, notifier, registry


# --- symbol -----------------------------------------------------------------

def test_occ_symbol_builds_and_rejects():
    assert so.occ_symbol("zzz", "Call", "150", "2027-01-15") == CALL
    assert so.occ_symbol("ZZZ", "p", "$22.5", "2026-12-18") == "ZZZ261218P00022500"
    assert so.describe(CALL) == "ZZZ 2027-01-15 $150 call"
    for bad in (("", "call", "1", "2027-01-15"), ("ZZZ", "straddle", "1", "2027-01-15"),
                ("ZZZ", "call", "0", "2027-01-15"), ("ZZZ", "call", "1.2345", "2027-01-15"),
                ("ZZZ", "call", "150", "Jan 2027"), ("ZZZ1", "call", "150", "2027-01-15"),
                ("ZZZ", "call", "NaN", "2027-01-15")):
        with pytest.raises(ValueError):
            so.occ_symbol(*bad)


# --- ledger: holdings, equity, marks ------------------------------------------

def test_option_holding_is_in_equity_but_never_in_positions(tmp_path):
    ledger, conn, _data, _queue = _world(tmp_path)
    ledger.set_position("ZZZ", Decimal(10), Decimal(100))
    assert ledger.get_account().equity == Decimal(10000 + 10 * 140)
    ledger.set_option_position(CALL, Decimal(2), Decimal("12.30"))
    # Valued at cost until the owner gives a price: 2 x 100 x 12.30.
    assert ledger.get_account().equity == Decimal(10000 + 1400) + Decimal(2460)
    assert ledger.get_account().cash == Decimal(10000)      # stating a holding moves no cash
    assert [p.ticker for p in ledger.get_positions()] == ["ZZZ"]
    [pos] = ledger.get_option_positions()
    assert (pos.ticker, pos.qty, pos.avg_entry_price, pos.market_value, pos.unrealized_pl) == (
        CALL, Decimal(2), Decimal("12.30"), Decimal(2460), Decimal(0))
    ledger.mark_option(CALL, Decimal("14.10"))
    assert ledger.get_account().equity == Decimal(11400) + Decimal(2820)
    assert ledger.get_option_positions()[0].unrealized_pl == Decimal(360)
    assert full_snapshot(ledger)["positions"].keys() == {"ZZZ"}
    notes = [r["note"] for r in conn.execute(
        "SELECT note FROM shadow_orders WHERE side = 'adjust' AND ticker = ?", (CALL,))]
    assert notes == [f"set_option_position {CALL}: none -> 2@12.30",
                     f"mark_option {CALL}: none -> 14.10"]
    assert ledger.get_orders(open_only=False) == []
    ledger.set_option_position(CALL, Decimal(3), Decimal("12.00"))      # keeps the mark
    assert option_snapshot(ledger, CALL) == {"contracts": "3", "avg_cost": "12.00",
                                             "last_price": "14.10"}
    ledger.set_option_position(CALL, Decimal(0), Decimal("12.00"))      # 0 removes it
    assert ledger.get_option_rows() == []


def test_ledger_rejects_bad_option_input(tmp_path):
    ledger, _conn, _data, _queue = _world(tmp_path)
    for args in ((CALL, Decimal("1.5"), Decimal(1)), (CALL, Decimal(-1), Decimal(1)),
                 (CALL, Decimal(1), Decimal(0)), ("ZZZ", Decimal(1), Decimal(1))):
        with pytest.raises(ValueError):
            ledger.set_option_position(*args)
    with pytest.raises(ValueError):
        ledger.mark_option(CALL, Decimal(1))                # nothing to mark
    with pytest.raises(ValueError):
        ledger.record_option_fill(CALL, "sell", Decimal(1), Decimal(1))
    with pytest.raises(ValueError):
        ledger.record_option_fill(CALL, "buy", Decimal(100), Decimal(50))   # > cash
    assert ledger.get_option_rows() == [] and ledger.get_account().cash == Decimal(10000)


def test_reported_fills_move_cash_and_average_the_cost(tmp_path):
    ledger, _conn, _data, _queue = _world(tmp_path)
    ledger.record_option_fill(CALL, "buy", Decimal(2), Decimal(10))
    ledger.record_option_fill(CALL, "buy", Decimal(1), Decimal(13))
    assert option_snapshot(ledger, CALL) == {"contracts": "3", "avg_cost": "11.000000",
                                             "last_price": "13"}
    assert ledger.get_account().cash == Decimal(10000 - 2000 - 1300)
    ledger.record_option_fill(CALL, "sell", Decimal(3), Decimal(15))
    assert ledger.get_option_rows() == []
    assert ledger.get_account().cash == Decimal(10000 - 3300 + 4500)


def test_view_flags_stale_marks_and_expired_contracts():
    row = {"symbol": CALL, "contracts": "2", "avg_cost": "12.30", "last_price": None,
           "last_price_ts": None}
    v = so.view(row, now=NOW)
    assert v["mark_source"] == "cost" and v["mark_stale"] and v["value"] == Decimal(2460)
    assert v["dte"] == 56 and not v["expired"] and v["shares_controlled"] == Decimal(200)
    assert so.mark_note(v) == "valued at cost: no price supplied yet"
    fresh = so.view({**row, "last_price": "14.10",
                     "last_price_ts": (NOW - timedelta(days=1)).isoformat()}, now=NOW)
    assert not fresh["mark_stale"] and so.mark_note(fresh) == "mark from 2026-11-19"
    old = so.view({**row, "last_price": "14.10",
                   "last_price_ts": (NOW - timedelta(days=9)).isoformat()}, now=NOW)
    assert old["mark_stale"] and so.mark_note(old).startswith("STALE mark from 2026-11-11")
    expired = so.view(row, now=datetime(2027, 2, 1, tzinfo=UTC))
    assert expired["expired"]
    text = so.line(expired, Decimal(100000), Decimal(140))
    assert "EXPIRED: still in the ledger" in text and "2 contracts (200 sh controlled" in text
    assert "ZZZ last $140.00, out of the money by $10.00" in text


# --- the confirmed-edit path --------------------------------------------------

def test_set_mark_and_remove_go_through_one_approval_each(tmp_path):
    call, ledger, _conn, queue, notifier, _reg = _tools(tmp_path)
    out = call("shadow_set_option_position", **C, contracts="2", avg_cost="12.30",
               last_price="14.10")
    assert "queued for your approval as #1: Set option ZZZ 2027-01-15 $150 call" in out
    assert ledger.get_option_rows() == [], "nothing is written before approval"
    row = queue.get(1)
    assert row["kind"] == "shadow_edit" and row["condition"] == "set_option"
    assert "Set option ZZZ 2027-01-15 $150 call" in notifier.sent[0][1]
    args = json.loads(row["snapshot"])["args"]
    assert current_shadow_state(ledger, "set_option", args) is None
    queue.approve(1)
    assert option_snapshot(ledger, CALL) == {"contracts": "2", "avg_cost": "12.30",
                                             "last_price": "14.10"}
    call("shadow_mark_option", **C, price="16")
    queue.approve(2)
    assert option_snapshot(ledger, CALL)["last_price"] == "16"
    call("shadow_remove_option_position", **C)
    queue.approve(3)
    assert ledger.get_option_rows() == []
    assert call("shadow_remove_option_position", **C).startswith("error: no option position")
    assert call("shadow_mark_option", **C, price="1").startswith("error: no option position")
    assert call("shadow_set_option_position", **C, contracts="2", avg_cost="0").startswith(
        "error: avg_cost must be > 0")


def test_a_stale_option_edit_stays_pending(tmp_path):
    call, ledger, _conn, queue, _notifier, _reg = _tools(tmp_path)
    call("shadow_set_option_position", **C, contracts="2", avg_cost="12.30")
    ledger.set_option_position(CALL, Decimal(1), Decimal(9))        # the book moved
    with pytest.raises(RevisionValidationError):
        queue.approve(1)
    assert queue.get(1)["status"] == "pending"
    assert option_snapshot(ledger, CALL)["contracts"] == "1"


def test_reported_trade_is_recorded_after_approval_and_evaluated(tmp_path):
    call, ledger, _conn, queue, _notifier, _reg = _tools(tmp_path)
    ledger.set_position("ZZZ", Decimal(50), Decimal(100))
    (tmp_path / "strategies" / "zzz-calls.yaml").write_text(
        STRATEGY.format(auth="notify", action="sell 10%"))
    out = call("shadow_record_option_trade", **C, side="buy", contracts="2", price="12.30")
    assert "queued for your approval as #1" in out
    assert "Record option trade: BUY 2 ZZZ 2027-01-15 $150 call @ $12.30 ($2,460.00)" in out
    for part in ("OPTION CHECK: ZZZ 2027-01-15 $150 call",
                 "EXPOSURE TO ZZZ: 50 shares held", "NOT delta-adjusted",
                 "days to expiry", "STRIKE: ZZZ last $140.00, 6.7% below the $150 strike",
                 "PLAN FOR THE UNDERLYING: zzz-calls [active/notify]: Hold the calls",
                 "IMPULSE CHECK"):
        assert part in out, part
    assert ledger.get_option_rows() == [] and ledger.get_account().cash == Decimal(10000)
    queue.approve(1)
    booked = option_snapshot(ledger, CALL)
    assert (booked["contracts"], Decimal(booked["avg_cost"]), booked["last_price"]) == (
        "2", Decimal("12.30"), "12.30")
    assert ledger.get_account().cash == Decimal(10000 - 2460)
    facts = call("option_position_check", **C)
    assert "PREMIUM AT RISK: $2,460.00 paid for this line" in facts
    assert "options controlling 200 shares net" in facts
    # What the ledger cannot hold is refused, not forced.
    assert call("shadow_record_option_trade", **C, side="sell", contracts="5",
                price="1").startswith("NOT QUEUED")
    assert call("shadow_record_option_trade", **C, side="buy", contracts="50",
                price="20").startswith("NOT QUEUED")
    assert len(queue.list()) == 0


# --- CSV import -----------------------------------------------------------

def test_csv_option_rows_parse_validate_and_import(tmp_path):
    rows, cash, errors = parse_shadow_csv(
        "ticker,qty,avg_cost\nZZZ,10,100\n"
        "OPTION,ZZZ,call,150,2027-01-15,2,12.30,14.10\n"
        "OPTION,zzz,P,22.5,2026-12-18,1,3\nCASH,500\n")
    assert errors == [] and cash == Decimal(500)
    assert rows == [
        {"ticker": "ZZZ", "qty": "10", "avg_cost": "100"},
        {"ticker": CALL, "qty": "2", "avg_cost": "12.30", "last_price": "14.10",
         "option": True},
        {"ticker": "ZZZ261218P00022500", "qty": "1", "avg_cost": "3", "last_price": None,
         "option": True}]
    _r, _c, errors = parse_shadow_csv(
        "OPTION,ZZZ,call,150,2027-01-15,2\nOPTION,ZZZ,call,150,soon,2,12.3\n"
        "OPTION,ZZZ,call,150,2027-01-15,1.5,12.3\nOPTION,ZZZ,call,150,2027-01-15,2,0\n"
        "OPTION,ZZZ,call,150,2027-01-15,2,5\nOPTION,ZZZ,call,150,2027-01-15,3,5\n")
    assert [e.split(":")[0] for e in errors] == ["line 1", "line 2", "line 3", "line 4",
                                                 "line 6"]
    assert "duplicate option" in errors[-1]

    ledger, _conn, _data, queue = _world(tmp_path)
    before = full_snapshot(ledger)
    queue.add_shadow_edit(op="import", ticker="", action="Import CSV",
                          args={"rows": rows, "cash": str(cash)}, before=before,
                          after=preview_import(before, rows, cash))
    queue.approve(1)
    assert [p.ticker for p in ledger.get_positions()] == ["ZZZ"]
    assert {r["symbol"] for r in ledger.get_option_rows()} == {CALL, "ZZZ261218P00022500"}
    assert option_snapshot(ledger, CALL)["last_price"] == "14.10"
    assert ledger.get_account().cash == Decimal(500)
    # A ledger reset clears the option lines too.
    queue.add_shadow_edit(op="reset", ticker="", action="Reset ledger", args={},
                          before=full_snapshot(ledger), after={"positions": {}, "cash": "0"})
    queue.approve(2)
    assert ledger.get_option_rows() == [] and ledger.get_positions() == []


# --- never proposed ---------------------------------------------------------

@pytest.mark.parametrize("auth,action", [("auto", "close_options"), ("confirm", "close_options"),
                                         ("auto", "buy_call $500"), ("confirm", "sell all"),
                                         ("auto", "sell 50%")])
def test_option_holdings_never_produce_an_order_or_a_proposal(tmp_path, auth, action):
    ledger, conn, data, _queue = _world(tmp_path)
    ledger.set_option_position(CALL, Decimal(2), Decimal("12.30"), Decimal("14.10"))
    # Even an expiring line is left alone: the expiry sweep never sees it.
    ledger.set_option_position("ZZZ261121C00100000", Decimal(1), Decimal(5))
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "zzz-calls.yaml").write_text(
        STRATEGY.format(auth=auth, action=action))
    store = StrategyStore(tmp_path / "strategies", conn, account="shadow")
    executor = SpyExecutor()
    queue = ReviewQueue(conn, executor, account="shadow")
    notifier = SpyNotifier()
    sentinel = Sentinel(store, data, ledger, executor, queue, notifier, account="shadow")
    before = ledger.get_option_rows()
    report = sentinel.run_once()
    assert executor.calls == [], "no order may be executed"
    assert queue.list() == [], "no order may be proposed"
    assert all(o.disposition in ("skipped", "error", "notified") for o in report.outcomes)
    assert ledger.get_option_rows() == before
    assert conn.execute("SELECT count(*) c FROM shadow_orders WHERE side IN ('buy','sell')"
                        ).fetchone()["c"] == 0


def test_no_option_tool_can_queue_an_order(tmp_path):
    call, ledger, _conn, queue, _notifier, registry = _tools(tmp_path)
    names = {s.name for s in registry.specs()}
    assert names == {"shadow_set_option_position", "shadow_remove_option_position",
                     "shadow_mark_option", "shadow_record_option_trade",
                     "option_position_check"}
    call("shadow_set_option_position", **C, contracts="2", avg_cost="12.30")
    call("shadow_record_option_trade", **C, side="buy", contracts="1", price="12")
    assert {r["kind"] for r in queue.list()} == {"shadow_edit"}
    assert all(r["intent"] is None for r in queue.list())
    assert "Never propose" in so.OPTION_HOLDINGS_CHAT_NOTE


# --- flag and paper -------------------------------------------------------

def test_flag_defaults_off_and_tools_are_shadow_only(tmp_path, monkeypatch):
    s = _settings()
    assert account_flag(s, so.FLAG, "shadow") is False
    s = _settings(shadow_option_holdings=True)
    assert account_flag(s, so.FLAG, "shadow") is True and account_flag(s, so.FLAG, "paper") is False
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))

    def sessions(sub, **flags):
        (tmp_path / sub).mkdir()
        client = _client(tmp_path / sub, monkeypatch, **flags)
        out = {}
        for account in ("paper", "shadow"):
            session = client.app.state.chat_services[account].session()
            out[account] = ([json.dumps(s.model_dump(), sort_keys=True, default=str)
                             for s in session.registry.specs()],
                            session.system_prompt.replace(str(tmp_path / sub), ""))
        client.__exit__(None, None, None)
        return out

    off, on = sessions("off"), sessions("on", shadow_option_holdings=True)
    assert on["paper"] == off["paper"], "paper tools and prompt are unchanged"
    assert "shadow_set_option_position" not in json.dumps(off["shadow"][0])
    assert "shadow_record_option_trade" in json.dumps(on["shadow"][0])
    assert so.OPTION_HOLDINGS_CHAT_NOTE in on["shadow"][1]
    assert "Option holdings." not in off["shadow"][1]


# --- marking on each surface -----------------------------------------------

def test_prompt_portfolio_tool_and_checkin_list_options_apart_from_shares(tmp_path):
    from allpath_trade.agent.context import build_system_prompt
    from allpath_trade.agent.readonly_tools import register_readonly_tools
    from allpath_trade.store.journal import TradeJournal

    ledger, conn, data, queue = _world(tmp_path)
    (tmp_path / "strategies").mkdir()
    store = StrategyStore(tmp_path / "strategies", conn, account="shadow")
    journal = TradeJournal(conn, account="shadow")

    def prompt():
        return build_system_prompt(identity="id", broker=ledger, journal=journal,
                                   strategies=store, queue=queue, account="shadow")

    registry = ToolRegistry()
    register_readonly_tools(registry, data=data, broker=ledger, journal=journal,
                            strategies=store, queue=queue)

    def portfolio():
        return registry.execute(ToolCall(id="x", name="get_portfolio", arguments={}))

    plain_prompt, plain_portfolio = prompt(), portfolio()
    plain_snapshot = live_snapshot(broker=ledger, data=data, breaker=None, now=NOW)
    assert "option" not in plain_prompt.split("## Current snapshot")[1]
    assert "options held" not in plain_portfolio and "Options in the ledger" not in plain_snapshot

    ledger.set_position("ZZZ", Decimal(10), Decimal(100))
    ledger.set_option_position(CALL, Decimal(2), Decimal("12.30"), Decimal("14.10"))
    text = prompt()
    assert "position: ZZZ qty=10" in text
    assert "option: ZZZ 2027-01-15 $150 call: 2 contracts (200 sh controlled" in text
    tool = portfolio()
    assert "  ZZZ: qty=10" in tool
    assert "options held (holdings only; never traded by the app):" in tool
    assert "mark $14.10/sh, value $2,820.00" in tool and "ZZZ last $140.00" in tool
    snapshot = live_snapshot(broker=ledger, data=data, breaker=None, now=NOW)
    assert "- ZZZ: 10 sh" in snapshot
    assert "Options in the ledger (holdings only; mark = the last price the owner" in snapshot
    assert "- ZZZ 2027-01-15 $150 call: 2 contracts" in snapshot
    assert "equity $14,220.00" in snapshot            # 10,000 + 1,400 + 2,820


def test_dashboard_shows_an_options_table_for_shadow_only(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    client = _client(tmp_path, monkeypatch)
    paper_before = client.get("/").text
    ledger = client.app.state.holder.get().accounts["shadow"].broker
    ledger.set_cash(Decimal(1000))
    ledger.set_option_position(CALL, Decimal(2), Decimal("12.30"))
    assert "Options held" not in client.get("/").text          # paper view
    assert client.get("/").text == paper_before
    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/").text
    assert "Options held" in body and "ZZZ 2027-01-15 $150 call" in body
    assert "valued at cost: no price supplied yet" in body and "$2,460.00" in body
    client.__exit__(None, None, None)


def test_settings_csv_import_accepts_option_rows_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    client = _client(tmp_path, monkeypatch)
    client.post("/account/switch", data={"account": "shadow"})
    csv_text = "ZZZ,10,100\nOPTION,ZZZ,call,150,2027-01-15,2,12.30,14.10\nCASH,500\n"
    preview = client.post("/settings/shadow/csv-preview",
                          files={"csv_file": ("p.csv", csv_text.encode(), "text/csv")})
    assert preview.status_code == 200 and CALL in preview.text
    bundle = client.app.state.holder.get().accounts["shadow"]
    assert bundle.queue.list() == []
    confirm = client.post("/settings/shadow/csv-confirm", data={"csv_text": csv_text},
                          follow_redirects=False)
    assert confirm.status_code in (200, 302, 303)
    [row] = bundle.queue.list()
    assert row["kind"] == "shadow_edit" and bundle.broker.get_option_rows() == []
    bundle.queue.approve(row["id"])
    assert option_snapshot(bundle.broker, CALL) == {"contracts": "2", "avg_cost": "12.30",
                                                    "last_price": "14.10"}
    assert client.get("/reviews").status_code == 200
    client.__exit__(None, None, None)
