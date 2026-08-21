from __future__ import annotations

from decimal import Decimal

import pytest

from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.memory_tools import register_memory_tools
from allpath_trade.agent.readonly_tools import register_readonly_tools
from allpath_trade.agent.shadow_tools import (
    apply_shadow_edit_factory,
    cash_snapshot,
    full_snapshot,
    order_snapshot,
    parse_shadow_csv,
    position_snapshot,
    preview_import,
    register_shadow_tools,
)
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import ToolCall
from allpath_trade.store.reviews import ReviewError, ReviewQueue, RevisionValidationError
from tests.test_shadow_ledger import FakeData, make_ledger


def call(reg, name, **kw):
    return reg.execute(ToolCall(id="x", name=name, arguments=kw))


def make(tmp_path, *, queue=None, answers=None, prices=None, cash="10000"):
    ledger, conn, data = make_ledger(tmp_path, prices=prices, cash=cash)
    reg = ToolRegistry()
    answers = list(answers or [])
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    register_shadow_tools(
        reg, ledger=ledger, queue=queue, conversation_id_fn=lambda: 1,
        confirm=confirm)
    return reg, ledger, conn, data, prompts


# --- CSV parsing -------------------------------------------------------------

def test_csv_parses_valid_rows_and_cash():
    rows, cash, errors = parse_shadow_csv("AAPL,10,150\nMSFT,5,300\nCASH,1000\n")
    assert errors == []
    assert cash == Decimal(1000)
    assert rows == [
        {"ticker": "AAPL", "qty": "10", "avg_cost": "150"},
        {"ticker": "MSFT", "qty": "5", "avg_cost": "300"},
    ]


def test_csv_skips_optional_header_row():
    rows, cash, errors = parse_shadow_csv("ticker,qty,avg_cost\nAAPL,10,150\n")
    assert errors == []
    assert rows == [{"ticker": "AAPL", "qty": "10", "avg_cost": "150"}]
    assert cash is None


def test_csv_reports_line_numbers_for_every_bad_row():
    text = "AAPL,10,150\nbad ticker!!,1,1\nMSFT,-5,10\nGOOG,1,0\n"
    _rows, _cash, errors = parse_shadow_csv(text)
    assert len(errors) == 3
    assert errors[0].startswith("line 2:")
    assert errors[1].startswith("line 3:")
    assert errors[2].startswith("line 4:")


def test_csv_bad_row_means_nothing_queued_worthy_is_returned():
    # A single bad line anywhere means the whole file is rejected -- the
    # caller (settings.py's shadow_csv_confirm) must check `errors` and
    # queue nothing at all, but this function itself still parses every row
    # so every error can be reported at once.
    _rows, _cash, errors = parse_shadow_csv("AAPL,10,150\nMSFT,abc,10\n")
    assert errors == ["line 2: invalid qty 'abc'"]


def test_csv_duplicate_ticker_is_an_error():
    _rows, _cash, errors = parse_shadow_csv("AAPL,10,150\nAAPL,5,100\n")
    assert any("duplicate" in e for e in errors)


def test_csv_two_cash_rows_is_an_error():
    _rows, _cash, errors = parse_shadow_csv("CASH,100\nCASH,200\n")
    assert any("only one CASH row" in e for e in errors)


def test_csv_negative_cash_is_an_error():
    _rows, _cash, errors = parse_shadow_csv("CASH,-1\n")
    assert any("cash must be >= 0" in e for e in errors)


def test_csv_empty_file_is_an_error():
    _rows, _cash, errors = parse_shadow_csv("")
    assert errors == ["no rows found in the file"]


def test_csv_wrong_column_count_is_an_error():
    _rows, _cash, errors = parse_shadow_csv("AAPL,10\n")
    assert any("expected 3 columns" in e for e in errors)


def test_preview_import_upserts_onto_before_snapshot():
    before = {"positions": {"AAPL": {"qty": "5", "avg_cost": "90"}}, "cash": "100"}
    rows = [{"ticker": "AAPL", "qty": "10", "avg_cost": "150"},
            {"ticker": "MSFT", "qty": "1", "avg_cost": "300"}]
    after = preview_import(before, rows, Decimal(500))
    assert after == {
        "positions": {
            "AAPL": {"qty": "10", "avg_cost": "150"},
            "MSFT": {"qty": "1", "avg_cost": "300"},
        },
        "cash": "500",
    }


def test_preview_import_leaves_cash_unchanged_when_csv_has_no_cash_row():
    before = {"positions": {}, "cash": "100"}
    after = preview_import(before, [{"ticker": "AAPL", "qty": "1", "avg_cost": "1"}], None)
    assert after["cash"] == "100"


# --- Snapshot helpers ---------------------------------------------------------

def test_position_snapshot_none_when_no_position(tmp_path):
    ledger, _, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(10)})
    assert position_snapshot(ledger, "AAPL") is None


def test_position_snapshot_reflects_current_state(tmp_path):
    ledger, _, _ = make_ledger(tmp_path)
    ledger.set_position("AAPL", Decimal(10), Decimal(100))
    assert position_snapshot(ledger, "aapl") == {"qty": "10", "avg_cost": "100"}


def test_cash_snapshot(tmp_path):
    ledger, _, _ = make_ledger(tmp_path, cash="500")
    assert cash_snapshot(ledger) == {"cash": "500"}


def test_order_snapshot_none_for_unknown_id(tmp_path):
    ledger, _, _ = make_ledger(tmp_path)
    assert order_snapshot(ledger, "999") is None


def test_order_snapshot_none_for_non_numeric_id(tmp_path):
    ledger, _, _ = make_ledger(tmp_path)
    assert order_snapshot(ledger, "not-a-number") is None


def test_order_snapshot_for_a_filled_order(tmp_path):
    from allpath_trade.broker.base import OrderIntent, OrderSide

    ledger, _, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    order = ledger.submit_order(
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(10), reason="x"))
    snap = order_snapshot(ledger, order.id)
    assert snap == {"ticker": "AAPL", "side": "buy", "qty": "10", "fill_price": "100"}


def test_full_snapshot(tmp_path):
    ledger, _, _ = make_ledger(tmp_path, cash="500")
    ledger.set_position("AAPL", Decimal(10), Decimal(100))
    assert full_snapshot(ledger) == {
        "positions": {"AAPL": {"qty": "10", "avg_cost": "100"}}, "cash": "500"}


# --- register_shadow_tools: web mode (queue given) -- must never mutate ----

def test_web_mode_set_position_queues_and_never_mutates_ledger(tmp_path):
    from allpath_trade.store.db import connect

    conn = connect(tmp_path / "q.db")
    queue = ReviewQueue(conn, None, account="shadow")
    reg, ledger, _, _, _ = make(tmp_path, queue=queue, prices={"AAPL": Decimal(10)})

    out = call(reg, "shadow_set_position", ticker="AAPL", qty="10", avg_cost="100")

    assert "queued for your approval as #" in out
    assert ledger.get_positions() == []  # never mutated
    [row] = queue.list()
    assert row["kind"] == "shadow_edit"
    assert row["action"] == "Set position AAPL"
    assert row["ticker"] == "AAPL"


def test_web_mode_set_cash_queues(tmp_path):
    from allpath_trade.store.db import connect

    conn = connect(tmp_path / "q.db")
    queue = ReviewQueue(conn, None, account="shadow")
    reg, ledger, _, _, _ = make(tmp_path, queue=queue)

    out = call(reg, "shadow_set_cash", amount="500")

    assert "queued for your approval as #" in out
    assert ledger.get_account().cash == Decimal(10000)  # unchanged (default cash)


def test_web_mode_remove_position_queues_and_does_not_mutate(tmp_path):
    from allpath_trade.store.db import connect

    conn = connect(tmp_path / "q.db")
    queue = ReviewQueue(conn, None, account="shadow")
    reg, ledger, _, _, _ = make(tmp_path, queue=queue)
    ledger.set_position("AAPL", Decimal(10), Decimal(100))

    out = call(reg, "shadow_remove_position", ticker="AAPL")

    assert "queued for your approval as #" in out
    assert ledger.get_positions()[0].ticker == "AAPL"  # still there


def test_web_mode_record_fill_queues(tmp_path):
    from allpath_trade.broker.base import OrderIntent, OrderSide
    from allpath_trade.store.db import connect

    conn = connect(tmp_path / "q.db")
    queue = ReviewQueue(conn, None, account="shadow")
    reg, ledger, _, _, _ = make(tmp_path, queue=queue, prices={"AAPL": Decimal(100)})
    order = ledger.submit_order(
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(10), reason="x"))

    out = call(reg, "shadow_record_fill", order_id=order.id, actual_price="90")

    assert "queued for your approval as #" in out
    row = ledger.get_order(order.id)
    assert row.filled_avg_price == Decimal(100)  # unchanged


# --- register_shadow_tools: terminal mode (queue=None) ----------------------

def test_terminal_mode_confirmed_applies_directly(tmp_path):
    reg, ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="10", avg_cost="100")
    assert out.startswith("applied:")
    [pos] = ledger.get_positions()
    assert pos.ticker == "AAPL" and pos.qty == Decimal(10)
    assert "Set position AAPL" in prompts[0]


def test_terminal_mode_declined_never_mutates(tmp_path):
    reg, ledger, _, _, _ = make(tmp_path, queue=None, answers=[False])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="10", avg_cost="100")
    assert out == "user declined"
    assert ledger.get_positions() == []


def test_terminal_mode_set_cash_confirmed(tmp_path):
    reg, ledger, _, _, _ = make(tmp_path, queue=None, answers=[True], cash="1")
    call(reg, "shadow_set_cash", amount="777")
    assert ledger.get_account().cash == Decimal(777)


# --- validation: rejected before ever prompting/queueing --------------------

def test_set_position_rejects_bad_ticker(tmp_path):
    reg, ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="not a ticker", qty="1", avg_cost="1")
    assert out.startswith("error:") and "ticker" in out
    assert prompts == []
    assert ledger.get_positions() == []


def test_set_position_rejects_negative_qty(tmp_path):
    reg, _ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="-1", avg_cost="1")
    assert "qty must be >= 0" in out
    assert prompts == []


def test_set_position_rejects_zero_avg_cost(tmp_path):
    reg, _ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="1", avg_cost="0")
    assert "avg_cost must be > 0" in out
    assert prompts == []


def test_set_position_rejects_unparseable_decimal(tmp_path):
    reg, _, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="abc", avg_cost="1")
    assert out.startswith("error:") and "qty" in out
    assert prompts == []


def test_set_cash_rejects_negative_amount(tmp_path):
    # T3 deferral: the negative-cash guard lands here, at tool-arg
    # validation time -- ShadowLedger.set_cash itself deliberately has none.
    reg, ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_cash", amount="-1")
    assert "cash must be >= 0" in out
    assert prompts == []
    assert ledger.get_account().cash == Decimal(10000)


def test_record_fill_rejects_non_positive_price(tmp_path):
    reg, _, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_record_fill", order_id="1", actual_price="0")
    assert "actual_price must be > 0" in out
    assert prompts == []


def test_record_fill_rejects_unknown_order(tmp_path):
    reg, _, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_record_fill", order_id="999", actual_price="10")
    assert out.startswith("error:") and "no filled shadow order" in out
    assert prompts == []


def test_remove_position_rejects_when_nothing_to_remove(tmp_path):
    reg, _, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_remove_position", ticker="AAPL")
    assert out.startswith("error:") and "no position" in out
    assert prompts == []


# --- registered only on the shadow registry ---------------------------------

def test_shadow_tools_not_present_unless_registered(tmp_path):
    # A paper/reflection-style registry (readonly + action + memory tools,
    # no register_shadow_tools call) must have none of the four tools.
    from allpath_trade.store.db import connect
    from allpath_trade.strategy.store import StrategyStore

    conn = connect(tmp_path / "t.db")
    (tmp_path / "strategies").mkdir()
    strategies = StrategyStore(tmp_path / "strategies", conn)
    reg = ToolRegistry()
    register_readonly_tools(reg, data=FakeData(), broker=None, journal=None,
                            strategies=strategies, queue=None)
    register_action_tools(reg, strategies=strategies, executor=None,
                          confirm=lambda _p: False)
    register_memory_tools(reg, memory=None, search=None)
    names = {spec.name for spec in reg.specs()}
    assert not any(n.startswith("shadow_") for n in names)


def test_shadow_tools_present_when_registered(tmp_path):
    reg, _, _, _, _ = make(tmp_path, queue=None, answers=[])
    names = {spec.name for spec in reg.specs()}
    assert names == {"shadow_set_position", "shadow_set_cash",
                     "shadow_remove_position", "shadow_record_fill"}


# --- apply_shadow_edit_factory (the approval-time applier) ------------------

def test_apply_set_position_writes_the_ledger(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path)
    apply = apply_shadow_edit_factory(ledger, conn)
    before = position_snapshot(ledger, "AAPL")
    apply("set_position", {"ticker": "AAPL", "qty": "10", "avg_cost": "100"}, before)
    assert position_snapshot(ledger, "AAPL") == {"qty": "10", "avg_cost": "100"}


def test_apply_set_cash_writes_the_ledger(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, cash="1")
    apply = apply_shadow_edit_factory(ledger, conn)
    before = cash_snapshot(ledger)
    apply("set_cash", {"amount": "999"}, before)
    assert ledger.get_account().cash == Decimal(999)


def test_apply_remove_position_writes_the_ledger(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path)
    ledger.set_position("AAPL", Decimal(10), Decimal(100))
    apply = apply_shadow_edit_factory(ledger, conn)
    before = position_snapshot(ledger, "AAPL")
    apply("remove_position", {"ticker": "AAPL"}, before)
    assert position_snapshot(ledger, "AAPL") is None


def test_apply_stale_before_state_raises_and_leaves_ledger_untouched(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path)
    apply = apply_shadow_edit_factory(ledger, conn)
    stale_before = {"qty": "999", "avg_cost": "1"}  # never matched reality

    with pytest.raises(RevisionValidationError, match="ledger changed"):
        apply("set_position", {"ticker": "AAPL", "qty": "10", "avg_cost": "100"},
              stale_before)

    assert position_snapshot(ledger, "AAPL") is None


def test_apply_set_position_stale_after_a_concurrent_change(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path)
    before = position_snapshot(ledger, "AAPL")  # None, recorded at "propose time"
    # The book moves in between (another approved edit, or a fill).
    ledger.set_position("AAPL", Decimal(5), Decimal(50))
    apply = apply_shadow_edit_factory(ledger, conn)

    with pytest.raises(RevisionValidationError):
        apply("set_position", {"ticker": "AAPL", "qty": "10", "avg_cost": "100"}, before)

    # Unchanged from the concurrent edit -- the stale proposal never applied.
    assert position_snapshot(ledger, "AAPL") == {"qty": "5", "avg_cost": "50"}


def test_apply_record_fill_intervening_sell_raises_revision_validation_error(tmp_path):
    from allpath_trade.broker.base import OrderIntent, OrderSide

    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    buy_order = ledger.submit_order(
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(10), reason="x"))
    ledger.submit_order(
        OrderIntent(ticker="AAPL", side=OrderSide.SELL, qty=Decimal(10), reason="x"))
    before = {"ticker": "AAPL", "side": "buy", "qty": "10", "fill_price": "100"}
    apply = apply_shadow_edit_factory(ledger, conn)

    with pytest.raises(RevisionValidationError, match="sold or"):
        apply("record_fill", {"order_id": buy_order.id, "actual_price": "90"}, before)


def test_apply_import_is_atomic_a_bad_row_applies_nothing(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, cash="1")
    ledger.set_position("MSFT", Decimal(1), Decimal(1))
    before = full_snapshot(ledger)
    apply = apply_shadow_edit_factory(ledger, conn)
    rows = [
        {"ticker": "AAPL", "qty": "10", "avg_cost": "100"},
        # avg_cost <= 0 -- ShadowLedger.set_position raises ValueError here,
        # which the applier turns into RevisionValidationError. This row
        # would never pass the propose-time tool's own validation, but the
        # applier must still refuse to half-apply if it's ever reached.
        {"ticker": "GOOG", "qty": "1", "avg_cost": "0"},
    ]

    with pytest.raises(RevisionValidationError):
        apply("import", {"rows": rows, "cash": None}, before)

    # AAPL (the first, otherwise-valid row) must NOT have been applied.
    assert position_snapshot(ledger, "AAPL") is None
    assert full_snapshot(ledger) == before


def test_apply_import_applies_every_row_and_optional_cash(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, cash="1")
    before = full_snapshot(ledger)
    apply = apply_shadow_edit_factory(ledger, conn)
    rows = [{"ticker": "AAPL", "qty": "10", "avg_cost": "100"},
            {"ticker": "MSFT", "qty": "1", "avg_cost": "300"}]

    apply("import", {"rows": rows, "cash": "5000"}, before)

    assert position_snapshot(ledger, "AAPL") == {"qty": "10", "avg_cost": "100"}
    assert position_snapshot(ledger, "MSFT") == {"qty": "1", "avg_cost": "300"}
    assert ledger.get_account().cash == Decimal(5000)


def test_apply_reset_clears_positions_and_zeroes_cash(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, cash="1000")
    ledger.set_position("AAPL", Decimal(10), Decimal(100))
    ledger.set_position("MSFT", Decimal(5), Decimal(50))
    before = full_snapshot(ledger)
    apply = apply_shadow_edit_factory(ledger, conn)

    apply("reset", {}, before)

    assert ledger.get_positions() == []
    assert ledger.get_account().cash == Decimal(0)


def test_apply_unknown_op_raises_revision_validation_error(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path)
    apply = apply_shadow_edit_factory(ledger, conn)
    with pytest.raises(RevisionValidationError, match="unknown shadow edit op"):
        apply("mystery", {}, None)


def test_apply_missing_arg_raises_revision_validation_error(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path)
    apply = apply_shadow_edit_factory(ledger, conn)
    with pytest.raises(RevisionValidationError):
        apply("set_position", {"ticker": "AAPL"}, None)  # missing qty/avg_cost


# --- add_shadow_edit is only valid on the shadow account's own queue -------

# --- Critical 1: Infinity/NaN/sNaN/1e999/-0 are rejected at every layer ----
# `Decimal("Infinity") < 0` is False and `> 0` is True -- every existing
# "must be >= 0"/"must be > 0" guard passes it straight through, and once
# written, ShadowLedger.get_account/get_positions (pydantic Decimal fields
# reject non-finite) raise ValidationError on every subsequent read forever.
# A bare `<`/`>` against NaN/sNaN raises InvalidOperation immediately,
# uncaught, which is what 500'd the CSV preview route. _parse_money closes
# both holes at the one shared choke point (agent/shadow_tools.py).

_BAD_NUMBERS = ["Infinity", "-Infinity", "NaN", "sNaN", "1e999", "-1e999"]


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_set_position_rejects_non_finite_and_oversized_qty(tmp_path, bad):
    reg, ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty=bad, avg_cost="1")
    assert out.startswith("error:")
    assert prompts == []
    assert ledger.get_positions() == []


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_set_position_rejects_non_finite_and_oversized_avg_cost(tmp_path, bad):
    reg, ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="1", avg_cost=bad)
    assert out.startswith("error:")
    assert prompts == []
    assert ledger.get_positions() == []


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_set_cash_rejects_non_finite_and_oversized(tmp_path, bad):
    reg, ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_cash", amount=bad)
    assert out.startswith("error:")
    assert prompts == []
    assert ledger.get_account().cash == Decimal(10000)


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_record_fill_rejects_non_finite_and_oversized_price(tmp_path, bad):
    from allpath_trade.broker.base import OrderIntent, OrderSide

    reg, ledger, _, _, prompts = make(tmp_path, queue=None, answers=[True],
                                      prices={"AAPL": Decimal(100)})
    order = ledger.submit_order(
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(10), reason="x"))
    out = call(reg, "shadow_record_fill", order_id=order.id, actual_price=bad)
    assert out.startswith("error:")
    assert prompts == []
    assert ledger.get_order(order.id).filled_avg_price == Decimal(100)


def test_set_position_rejects_magnitude_over_1e12(tmp_path):
    reg, ledger, _, _, _ = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="1", avg_cost="1e13")
    assert out.startswith("error:") and "avg_cost" in out
    assert ledger.get_positions() == []


def test_set_position_normalizes_negative_zero_qty(tmp_path):
    # -0 < 0 is False (a legit qty=0 no-op-ish "flatten to zero" case) --
    # but the STORED value must be plain "0", not the surprising literal
    # "-0", so a later snapshot string-compare (staleness) is predictable.
    reg, ledger, _, _, _ = make(tmp_path, queue=None, answers=[True])
    out = call(reg, "shadow_set_position", ticker="AAPL", qty="-0", avg_cost="1")
    assert out.startswith("applied:")
    [pos] = ledger.get_positions()
    assert str(pos.qty) == "0"


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_csv_rejects_non_finite_and_oversized_qty(bad):
    _rows, _cash, errors = parse_shadow_csv(f"AAPL,{bad},150\n")
    assert len(errors) == 1 and "line 1" in errors[0]


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_csv_rejects_non_finite_and_oversized_cash(bad):
    _rows, _cash, errors = parse_shadow_csv(f"CASH,{bad}\n")
    assert len(errors) == 1 and "line 1" in errors[0]


def test_csv_caps_row_count():
    text = "\n".join(f"T{i},1,1" for i in range(2001))
    _rows, _cash, errors = parse_shadow_csv(text)
    assert any("too many rows" in e for e in errors)


def test_csv_caps_file_size():
    text = "AAPL,10,150\n" + ("x" * 1_000_001)
    _rows, _cash, errors = parse_shadow_csv(text)
    assert any("too large" in e for e in errors)


@pytest.mark.parametrize("bad", _BAD_NUMBERS)
def test_apply_set_position_rejects_non_finite_qty_leaves_pending(tmp_path, bad):
    ledger, conn, _ = make_ledger(tmp_path)
    apply = apply_shadow_edit_factory(ledger, conn)
    before = position_snapshot(ledger, "AAPL")

    with pytest.raises(RevisionValidationError):
        apply("set_position", {"ticker": "AAPL", "qty": bad, "avg_cost": "1"}, before)

    assert position_snapshot(ledger, "AAPL") is None  # never written


def test_apply_import_rejects_non_finite_row_and_writes_nothing(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, cash="1")
    before = full_snapshot(ledger)
    apply = apply_shadow_edit_factory(ledger, conn)
    rows = [
        {"ticker": "AAPL", "qty": "10", "avg_cost": "100"},
        {"ticker": "GOOG", "qty": "Infinity", "avg_cost": "1"},
    ]

    with pytest.raises(RevisionValidationError):
        apply("import", {"rows": rows, "cash": None}, before)

    assert position_snapshot(ledger, "AAPL") is None
    assert full_snapshot(ledger) == before


def test_apply_set_cash_rejects_nan_leaves_pending(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, cash="500")
    apply = apply_shadow_edit_factory(ledger, conn)
    before = cash_snapshot(ledger)

    with pytest.raises(RevisionValidationError):
        apply("set_cash", {"amount": "NaN"}, before)

    assert ledger.get_account().cash == Decimal(500)  # untouched


# --- add_shadow_edit is only valid on the shadow account's own queue -------

def test_add_shadow_edit_rejected_on_a_non_shadow_queue(tmp_path):
    from allpath_trade.store.db import connect

    paper_queue = ReviewQueue(connect(tmp_path / "t.db"), None)
    with pytest.raises(ReviewError, match="shadow account"):
        paper_queue.add_shadow_edit(
            op="set_cash", ticker="", action="Set cash", args={"amount": "1"},
            before={"cash": "0"}, after={"cash": "1"})
