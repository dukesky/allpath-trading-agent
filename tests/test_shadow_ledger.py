from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from allpath_trade import scheduler as scheduler_module
from allpath_trade.broker import shadow as shadow_module
from allpath_trade.broker.base import OrderIntent, OrderSide, OrderStatus
from allpath_trade.broker.shadow import ShadowLedger
from allpath_trade.data import yf as yf_module
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.data.yf import YFinanceSource
from allpath_trade.store.db import connect


class FakeData(DataSource):
    """Quote source with per-ticker prices and a `fail` set of tickers that
    raise instead of returning a quote -- a raising DataSource is exactly
    how ShadowLedger is told "no quote" per the spec."""

    def __init__(self, prices: dict[str, Decimal] | None = None,
                fail: set[str] | None = None) -> None:
        self.prices = dict(prices or {})
        self.fail = set(fail or set())

    def get_quote(self, ticker: str) -> Quote:
        if ticker in self.fail:
            raise RuntimeError(f"no data for {ticker}")
        if ticker not in self.prices:
            raise RuntimeError(f"unknown ticker {ticker}")
        return Quote(ticker=ticker, price=self.prices[ticker], as_of=datetime.now(UTC))

    def get_bars(self, ticker: str, days: int = 365) -> list:
        return []


def make_ledger(tmp_path, prices=None, fail=None, cash="10000"):
    conn = connect(tmp_path / "t.db")
    data = FakeData(prices=prices, fail=fail)
    ledger = ShadowLedger(conn, data)
    if cash is not None:
        ledger.set_cash(Decimal(cash))
    return ledger, conn, data


def buy(ticker="AAPL", qty=None, notional=None, reason="test buy"):
    return OrderIntent(ticker=ticker, side=OrderSide.BUY, qty=qty, notional=notional,
                       reason=reason)


def sell(ticker="AAPL", qty=None, notional=None, reason="test sell"):
    return OrderIntent(ticker=ticker, side=OrderSide.SELL, qty=qty, notional=notional,
                       reason=reason)


# -- submit_order: buy ------------------------------------------------------

def test_buy_creates_position_and_deducts_cash(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    order = ledger.submit_order(buy(qty=Decimal(10)))
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == Decimal(10)
    assert order.filled_avg_price == Decimal(100)

    acct = ledger.get_account()
    assert acct.cash == Decimal(9000)

    [pos] = ledger.get_positions()
    assert pos.ticker == "AAPL"
    assert pos.qty == Decimal(10)
    assert pos.avg_entry_price == Decimal("100.000000")


def test_buy_insufficient_cash_rejected(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="500")
    order = ledger.submit_order(buy(qty=Decimal(10)))  # needs 1000, has 500
    assert order.status == OrderStatus.REJECTED
    assert order.filled_qty == Decimal(0)
    assert order.filled_avg_price is None
    assert ledger.get_account().cash == Decimal(500)
    assert ledger.get_positions() == []

    row = conn.execute("SELECT * FROM shadow_orders WHERE side = 'buy'").fetchone()
    assert row["status"] == "rejected"
    assert "insufficient cash" in row["note"]
    assert row["fill_price"] is None


def test_weighted_average_cost_on_second_buy(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))  # avg 100, qty 10
    ledger.submit_order(buy(qty=Decimal(10), reason="second"))
    # price is still 100 -- switch the fake quote for the 2nd call instead
    row = conn.execute("SELECT qty, avg_cost FROM shadow_positions").fetchone()
    assert row["qty"] == "20"
    assert row["avg_cost"] == "100.000000"


def test_weighted_average_cost_different_prices(tmp_path):
    ledger, conn, data = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="100000")
    ledger.submit_order(buy(qty=Decimal(10)))  # 10 @ 100
    data.prices["AAPL"] = Decimal(200)
    ledger.submit_order(buy(qty=Decimal(10), reason="second"))  # 10 @ 200
    row = conn.execute("SELECT qty, avg_cost FROM shadow_positions").fetchone()
    assert row["qty"] == "20"
    # (10*100 + 10*200) / 20 = 150 exactly -- exact string, no float artifact.
    assert row["avg_cost"] == "150.000000"


def test_notional_buy_fractional_qty_decimal_exact(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(3)}, cash="1000")
    order = ledger.submit_order(buy(notional=Decimal(100)))
    # 100 / 3 = 33.333333333... -> quantized 6dp ROUND_HALF_UP = 33.333333
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == Decimal("33.333333")
    assert order.filled_avg_price == Decimal(3)
    # cost = 33.333333 * 3 = 99.999999 exactly -- no float rounding artifact.
    acct = ledger.get_account()
    assert acct.cash == Decimal("900.000001")
    row = conn.execute("SELECT qty FROM shadow_positions").fetchone()
    assert row["qty"] == "33.333333"


# -- submit_order: sell -------------------------------------------------------

def test_sell_reduces_position_and_credits_cash(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))
    order = ledger.submit_order(sell(qty=Decimal(4)))
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == Decimal(4)
    assert ledger.get_account().cash == Decimal(9000) + Decimal(400)
    [pos] = ledger.get_positions()
    assert pos.qty == Decimal(6)
    assert pos.avg_entry_price == Decimal("100.000000")  # unchanged by a sell


def test_sell_to_exactly_zero_removes_position_row(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))
    ledger.submit_order(sell(qty=Decimal(10)))
    assert ledger.get_positions() == []
    row = conn.execute("SELECT * FROM shadow_positions WHERE ticker = 'AAPL'").fetchone()
    assert row is None


def test_oversell_rejected(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(5)))
    order = ledger.submit_order(sell(qty=Decimal(10)))
    assert order.status == OrderStatus.REJECTED
    assert "oversell" in conn.execute(
        "SELECT note FROM shadow_orders WHERE side = 'sell'").fetchone()["note"]
    [pos] = ledger.get_positions()
    assert pos.qty == Decimal(5)  # unchanged


def test_short_sell_no_position_rejected(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    order = ledger.submit_order(sell(qty=Decimal(1)))
    assert order.status == OrderStatus.REJECTED
    assert ledger.get_positions() == []


# -- no quote -----------------------------------------------------------------

def test_no_quote_rejected_no_book_entry(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={}, fail={"ZZZZ"})
    order = ledger.submit_order(buy(ticker="ZZZZ", qty=Decimal(10)))
    assert order.status == OrderStatus.REJECTED
    assert ledger.get_positions() == []
    assert ledger.get_account().cash == Decimal(10000)  # untouched

    row = conn.execute("SELECT * FROM shadow_orders WHERE ticker = 'ZZZZ'").fetchone()
    assert row is not None  # audit trail row still exists
    assert row["status"] == "rejected"
    assert "no quote" in row["note"]
    assert row["fill_price"] is None
    assert row["filled_at"] is None


def test_no_quote_does_not_touch_last_price_of_other_tickers(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, fail={"ZZZZ"})
    ledger.submit_order(buy(ticker="AAPL", qty=Decimal(1)))
    ledger.submit_order(buy(ticker="ZZZZ", qty=Decimal(1)))
    row = conn.execute(
        "SELECT last_price FROM shadow_positions WHERE ticker = 'AAPL'").fetchone()
    assert row["last_price"] == "100"


def test_rejected_buy_still_refreshes_last_price_if_position_exists(tmp_path):
    ledger, conn, data = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="150")
    ledger.submit_order(buy(qty=Decimal(1)))  # succeeds, cash now 50
    data.prices["AAPL"] = Decimal(120)
    order = ledger.submit_order(buy(qty=Decimal(10), reason="too big"))  # rejected
    assert order.status == OrderStatus.REJECTED
    row = conn.execute(
        "SELECT last_price FROM shadow_positions WHERE ticker = 'AAPL'").fetchone()
    assert row["last_price"] == "120"  # refreshed despite rejection


# -- valuation: fresh / stale / cost-basis fallback --------------------------

def test_valuation_uses_fresh_quote(tmp_path):
    ledger, _conn, data = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))
    data.prices["AAPL"] = Decimal(150)
    [pos] = ledger.get_positions()
    assert pos.market_value == Decimal(1500)
    assert pos.unrealized_pl == Decimal(500)
    acct = ledger.get_account()
    assert acct.equity == acct.cash + Decimal(1500)


def test_valuation_falls_back_to_last_price_when_quote_fails(tmp_path):
    ledger, _conn, data = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))  # last_price recorded as 100
    data.fail.add("AAPL")  # quote source now fails
    [pos] = ledger.get_positions()
    assert pos.market_value == Decimal(1000)  # priced at stale last_price=100


def test_valuation_falls_back_to_avg_cost_when_no_last_price(tmp_path):
    ledger, _conn, data = make_ledger(tmp_path, prices={})
    ledger.set_position("AAPL", Decimal(10), Decimal(80))  # never filled -> no last_price
    data.fail.add("AAPL")
    [pos] = ledger.get_positions()
    assert pos.market_value == Decimal(800)  # priced at avg_cost=80
    assert pos.unrealized_pl == Decimal(0)


# -- equity daily upsert -------------------------------------------------------

def test_get_account_upserts_todays_equity_point(tmp_path, monkeypatch):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    monkeypatch.setattr(shadow_module, "today_et_date", lambda: "2026-08-20")
    ledger.submit_order(buy(qty=Decimal(10)))
    ledger.get_account()
    row = conn.execute(
        "SELECT * FROM shadow_equity_daily WHERE date = '2026-08-20'").fetchone()
    assert row is not None
    assert row["equity"] == "10000"  # 9000 cash + 1000 position, priced at 100
    assert row["cash"] == "9000"

    # A second call on the SAME (frozen) day updates the row in place rather
    # than accumulating a second one.
    ledger.get_account()
    rows = list(conn.execute(
        "SELECT * FROM shadow_equity_daily WHERE date = '2026-08-20'"))
    assert len(rows) == 1


def test_get_account_survives_equity_write_failure(tmp_path, monkeypatch):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})

    def _boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(shadow_module, "today_et_date", _boom)
    acct = ledger.get_account()  # must not raise -- best-effort write
    assert acct.cash == Decimal(10000)


def test_get_equity_history_ordering_and_cutoff(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={})
    conn.execute(
        "INSERT INTO shadow_equity_daily (date, equity, cash) VALUES"
        " ('2020-01-01', '100', '100'),"
        " ('2026-08-18', '200', '200'),"
        " ('2026-08-19', '300', '300')")
    conn.commit()
    history = ledger.get_equity_history(days=30)
    dates = [d.date().isoformat() for d, _ in history]
    assert dates == ["2026-08-18", "2026-08-19"] or dates[-2:] == ["2026-08-18", "2026-08-19"]
    # oldest first
    assert history == sorted(history, key=lambda t: t[0])


# -- get_order / get_orders / cancel_order ------------------------------------

def test_get_order_roundtrip(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    order = ledger.submit_order(buy(qty=Decimal(10)))
    fetched = ledger.get_order(order.id)
    assert fetched.id == order.id
    assert fetched.status == OrderStatus.FILLED
    assert fetched.filled_qty == Decimal(10)
    assert fetched.filled_avg_price == Decimal(100)


def test_get_order_not_found_raises(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={})
    with pytest.raises(LookupError):
        ledger.get_order("999")


def test_get_orders_open_only_always_empty(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))
    assert ledger.get_orders(open_only=True) == []


def test_get_orders_all_excludes_adjust_rows(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))
    ledger.set_position("MSFT", Decimal(5), Decimal(50))  # writes an 'adjust' row
    orders = ledger.get_orders(open_only=False)
    assert len(orders) == 1
    assert orders[0].ticker == "AAPL"


def test_cancel_order_is_noop(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    order = ledger.submit_order(buy(qty=Decimal(10)))
    assert ledger.cancel_order(order.id) is None
    # nothing changed
    still = ledger.get_order(order.id)
    assert still.status == OrderStatus.FILLED


# -- ledger mutation helpers + audit trail ------------------------------------

def test_set_position_writes_audit_row(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={})
    ledger.set_position("AAPL", Decimal(10), Decimal(90))
    [pos] = ledger.get_positions()
    assert pos.qty == Decimal(10)
    assert pos.avg_entry_price == Decimal(90)
    row = conn.execute(
        "SELECT * FROM shadow_orders WHERE side = 'adjust' AND ticker = 'AAPL'").fetchone()
    assert row is not None
    assert "set_position" in row["note"]


def test_set_cash_writes_audit_row(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={}, cash="1000")
    ledger.set_cash(Decimal(5000))
    assert ledger.get_account().cash == Decimal(5000)
    rows = list(conn.execute("SELECT * FROM shadow_orders WHERE side = 'adjust'"))
    assert any("set_cash" in r["note"] for r in rows)


def test_remove_position_writes_audit_row(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={})
    ledger.set_position("AAPL", Decimal(10), Decimal(90))
    ledger.remove_position("AAPL")
    assert ledger.get_positions() == []
    row = conn.execute(
        "SELECT * FROM shadow_orders WHERE side = 'adjust' AND note LIKE 'remove_position%'"
    ).fetchone()
    assert row is not None


def test_remove_position_noop_when_absent_still_audits(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={})
    ledger.remove_position("NOPE")  # never existed
    row = conn.execute(
        "SELECT * FROM shadow_orders WHERE side = 'adjust' AND ticker = 'NOPE'").fetchone()
    assert row is not None
    assert "none (no-op)" in row["note"]


# -- record_fill correction math -----------------------------------------------

def test_record_fill_corrects_buy_cash_and_avg_cost(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(10)))  # cost 1000, cash -> 9000
    ledger.record_fill(order.id, Decimal(90))  # actual fill was 90, not 100
    # revert 1000, recharge 900 -> net +100 vs the original charge
    assert ledger.get_account().cash == Decimal(9100)
    row = conn.execute("SELECT avg_cost FROM shadow_positions WHERE ticker='AAPL'").fetchone()
    assert row["avg_cost"] == "90.000000"
    order_row = conn.execute(
        "SELECT fill_price FROM shadow_orders WHERE id = ?", (int(order.id),)).fetchone()
    assert order_row["fill_price"] == "90"
    audit = conn.execute(
        "SELECT * FROM shadow_orders WHERE side = 'adjust' AND note LIKE 'record_fill%'"
    ).fetchone()
    assert audit is not None


def test_record_fill_corrects_buy_with_later_second_buy(tmp_path):
    ledger, conn, data = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="100000")
    first = ledger.submit_order(buy(qty=Decimal(10)))  # 10 @ 100
    data.prices["AAPL"] = Decimal(200)
    ledger.submit_order(buy(qty=Decimal(10), reason="second"))  # 10 @ 200 -> avg 150, qty 20
    ledger.record_fill(first.id, Decimal(50))  # correct the FIRST fill to 50
    # old_total_value = 20*150 - 10*100 = 3000-1000 = 2000
    # new_total_value = 2000 + 10*50 = 2500; new_avg = 2500/20 = 125
    row = conn.execute("SELECT qty, avg_cost FROM shadow_positions WHERE ticker='AAPL'"
                       ).fetchone()
    assert row["qty"] == "20"
    assert row["avg_cost"] == "125.000000"


def test_record_fill_corrects_sell_cash_only(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    ledger.submit_order(buy(qty=Decimal(10)))  # cash -> 9000
    sell_order = ledger.submit_order(sell(qty=Decimal(5)))  # proceeds 500, cash -> 9500
    ledger.record_fill(sell_order.id, Decimal(120))  # actual sale was 120, not 100
    # revert 500, recredit 600 -> net +100
    assert ledger.get_account().cash == Decimal(9600)
    [pos] = ledger.get_positions()
    assert pos.qty == Decimal(5)  # unaffected -- only cash moves on a sell correction


def test_record_fill_reversal_impossible_raises(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(10)))
    ledger.submit_order(sell(qty=Decimal(10)))  # position fully closed & row removed
    # The intervening sell is now caught by the earlier, more general
    # "has this ticker been sold/adjusted since this fill" guard (Critical
    # 1) before the remaining_qty<0 check is ever reached -- same root
    # cause, an earlier and more precise diagnosis.
    with pytest.raises(ValueError, match="sold or.*manually adjusted"):
        ledger.record_fill(order.id, Decimal(50))
    # nothing was mutated by the failed attempt
    assert ledger.get_positions() == []


def test_record_fill_unknown_order_raises(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={})
    with pytest.raises(ValueError):
        ledger.record_fill("999", Decimal(50))


def test_record_fill_rejects_non_filled_order(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="1")
    order = ledger.submit_order(buy(qty=Decimal(10)))  # rejected: insufficient cash
    assert order.status == OrderStatus.REJECTED
    with pytest.raises(ValueError):
        ledger.record_fill(order.id, Decimal(50))


# -- Decimal exactness across the board ---------------------------------------

def test_no_float_artifacts_anywhere_in_stored_strings(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal("33.33")}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(3)))
    assert order.filled_avg_price == Decimal("33.33")
    cash_row = conn.execute("SELECT cash FROM shadow_cash WHERE id = 1").fetchone()
    # 10000 - 3*33.33 = 10000 - 99.99 = 9900.01 exactly.
    assert cash_row["cash"] == "9900.01"


def test_today_et_date_is_the_real_scheduler_helper(tmp_path):
    # Sanity check the ledger imports the real shared ET-date helper (not a
    # private reimplementation) -- confirms the frozen-date monkeypatch
    # tests above are patching the thing get_account() actually calls.
    assert shadow_module.today_et_date is scheduler_module.today_et_date


# -- helpers for the book-state snapshot assertions below ---------------------

def _snapshot(conn):
    """A byte-identical snapshot of every table record_fill can touch, for
    asserting a failed correction attempt mutated nothing."""
    return (
        [dict(r) for r in conn.execute(
            "SELECT * FROM shadow_positions ORDER BY ticker")],
        dict(conn.execute("SELECT * FROM shadow_cash WHERE id = 1").fetchone()),
        [dict(r) for r in conn.execute("SELECT * FROM shadow_orders ORDER BY id")],
    )


# -- Critical 1: record_fill guards against an intervening sell/adjust --------

def test_record_fill_blocked_by_intervening_partial_sell_across_two_lots(tmp_path):
    """Reproduces the multi-lot + partial-sell phantom-basis corruption:
    two buys blend into one position, a partial sell trims it (the sold
    shares can't be attributed to either specific lot), and a correction to
    the FIRST lot must now be refused rather than silently mis-blend a
    phantom cost basis into the survivors."""
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="100000")
    first = ledger.submit_order(buy(qty=Decimal(10)))  # 10 @ 100
    ledger.submit_order(buy(qty=Decimal(10), reason="second"))  # 10 @ 100 -> qty 20
    ledger.submit_order(sell(qty=Decimal(5), reason="partial trim"))  # qty -> 15
    before = _snapshot(conn)
    with pytest.raises(ValueError, match="sold or.*manually adjusted"):
        ledger.record_fill(first.id, Decimal(50))
    assert _snapshot(conn) == before  # books byte-identical after the raise


def test_record_fill_blocked_after_position_closed_and_reopened(tmp_path):
    """Reproduces the closed-and-reopened corruption: the original position
    is fully sold (row deleted), a brand new unrelated position is opened
    later, and correcting the original (now historical) fill must not
    silently overwrite the new, unrelated position's avg_cost."""
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="100000")
    original = ledger.submit_order(buy(qty=Decimal(10)))  # 10 @ 100
    ledger.submit_order(sell(qty=Decimal(10)))  # position fully closed, row removed
    ledger.submit_order(buy(qty=Decimal(3), reason="unrelated reopen"))  # 3 @ 100
    before = _snapshot(conn)
    with pytest.raises(ValueError, match="sold or.*manually adjusted"):
        ledger.record_fill(original.id, Decimal(300))  # would have overwritten avg_cost
    assert _snapshot(conn) == before  # the live, unrelated position is untouched


def test_record_fill_blocked_after_manual_set_position(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(10)))
    ledger.set_position("AAPL", Decimal(10), Decimal(77))  # manual correction
    before = _snapshot(conn)
    with pytest.raises(ValueError, match="sold or.*manually adjusted"):
        ledger.record_fill(order.id, Decimal(50))
    assert _snapshot(conn) == before


def test_record_fill_blocked_after_remove_position(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(10)))
    ledger.remove_position("AAPL")
    before = _snapshot(conn)
    with pytest.raises(ValueError, match="sold or.*manually adjusted"):
        ledger.record_fill(order.id, Decimal(50))
    assert _snapshot(conn) == before


# -- happy paths that must survive the new guard -------------------------------

def test_record_fill_double_correction_of_same_order_still_works(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(10)))  # 10 @ 100, cash -> 9000
    ledger.record_fill(order.id, Decimal(90))  # first correction
    ledger.record_fill(order.id, Decimal(80))  # second correction on the SAME order
    row = conn.execute("SELECT avg_cost FROM shadow_positions WHERE ticker='AAPL'").fetchone()
    assert row["avg_cost"] == "80.000000"
    order_row = conn.execute(
        "SELECT fill_price FROM shadow_orders WHERE id = ?", (int(order.id),)).fetchone()
    assert order_row["fill_price"] == "80"
    # cash: started 10000, cost 1000 -> 9000; corrections net qty*(old-actual)
    # each time: after both, cash reflects a net fill price of 80.
    assert ledger.get_account().cash == Decimal(10000) - Decimal(10) * Decimal(80)


def test_record_fill_second_lot_present_still_works(tmp_path):
    # Same scenario as test_record_fill_corrects_buy_with_later_second_buy
    # (no sell/adjust in between -- only a second buy) -- must stay green.
    ledger, conn, data = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="100000")
    first = ledger.submit_order(buy(qty=Decimal(10)))  # 10 @ 100
    data.prices["AAPL"] = Decimal(200)
    ledger.submit_order(buy(qty=Decimal(10), reason="second"))  # 10 @ 200 -> avg 150, qty 20
    ledger.record_fill(first.id, Decimal(50))
    row = conn.execute("SELECT qty, avg_cost FROM shadow_positions WHERE ticker='AAPL'"
                       ).fetchone()
    assert row["qty"] == "20"
    assert row["avg_cost"] == "125.000000"


def test_record_fill_sell_correction_still_works(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    ledger.submit_order(buy(qty=Decimal(10)))
    sell_order = ledger.submit_order(sell(qty=Decimal(5)))
    ledger.record_fill(sell_order.id, Decimal(120))
    assert ledger.get_account().cash == Decimal(9600)


# -- Important 2: _upsert_equity_daily must nest inside an outer transaction --

def test_get_account_equity_upsert_nests_inside_outer_transaction(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)})
    ledger.submit_order(buy(qty=Decimal(10)))

    class Boom(Exception):
        pass

    # If _upsert_equity_daily's write broke savepoint nesting (a bare
    # commit() ending the outer transaction()'s SAVEPOINT early), the outer
    # `with conn.transaction()`'s cleanup on this exception would itself
    # raise sqlite3.OperationalError ("no such savepoint"), masking Boom.
    # pytest.raises(Boom) below only passes if that does NOT happen.
    with pytest.raises(Boom), conn.transaction():
        ledger.get_account()
        raise Boom("outer failure")

    # Connection must still be perfectly usable afterward.
    assert conn.execute("SELECT 1").fetchone() is not None


# -- Important 3: record_fill rejects non-positive prices ---------------------

def test_record_fill_rejects_zero_or_negative_price(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(10)))
    with pytest.raises(ValueError, match="positive"):
        ledger.record_fill(order.id, Decimal(0))
    with pytest.raises(ValueError, match="positive"):
        ledger.record_fill(order.id, Decimal(-5))
    # unaffected by the rejected attempts
    [pos] = ledger.get_positions()
    assert pos.avg_entry_price == Decimal("100.000000")


# -- Minor 4: set_position guards ----------------------------------------------

def test_set_position_rejects_negative_qty(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={})
    with pytest.raises(ValueError):
        ledger.set_position("AAPL", Decimal(-1), Decimal(90))


def test_set_position_rejects_non_positive_avg_cost(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={})
    with pytest.raises(ValueError):
        ledger.set_position("AAPL", Decimal(10), Decimal(0))
    with pytest.raises(ValueError):
        ledger.set_position("AAPL", Decimal(10), Decimal(-5))


def test_set_position_allows_zero_qty_with_positive_avg_cost(tmp_path):
    ledger, _conn, _ = make_ledger(tmp_path, prices={})
    ledger.set_position("AAPL", Decimal(0), Decimal(90))  # not rejected
    [pos] = ledger.get_positions()
    assert pos.qty == Decimal(0)


# -- Minor 5: notional too small for one 6dp share -----------------------------

def test_notional_too_small_for_one_share_rejected_no_phantom_row(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(notional=Decimal("0.0000001")))
    assert order.status == OrderStatus.REJECTED
    assert order.filled_qty == Decimal(0)
    row = conn.execute("SELECT note FROM shadow_orders WHERE side = 'buy'").fetchone()
    assert "too small" in row["note"]
    assert ledger.get_positions() == []
    assert ledger.get_account().cash == Decimal(10000)
    pos_row = conn.execute(
        "SELECT * FROM shadow_positions WHERE ticker = 'AAPL'").fetchone()
    assert pos_row is None  # no phantom 0-share row


# -- Minor 7 / 8: get_equity_history ET-anchored cutoff + zero-equity filter --

def test_get_equity_history_cutoff_uses_et_calendar_date(tmp_path, monkeypatch):
    ledger, conn, _ = make_ledger(tmp_path, prices={})
    conn.execute(
        "INSERT INTO shadow_equity_daily (date, equity, cash) VALUES"
        " ('2026-08-14', '50', '50'),"
        " ('2026-08-15', '100', '100'),"
        " ('2026-08-16', '200', '200')")
    conn.commit()
    # Freeze "today" (ET) to 2026-08-20 regardless of the real wall clock --
    # get_equity_history must derive its cutoff from the same today_et_date
    # helper _upsert_equity_daily uses, not a raw UTC datetime.now() date.
    monkeypatch.setattr(shadow_module, "today_et_date", lambda: "2026-08-20")
    history = ledger.get_equity_history(days=5)  # cutoff = 2026-08-15
    dates = [d.date().isoformat() for d, _ in history]
    assert dates == ["2026-08-15", "2026-08-16"]


def test_get_equity_history_filters_zero_equity_days(tmp_path, monkeypatch):
    ledger, conn, _ = make_ledger(tmp_path, prices={})
    conn.execute(
        "INSERT INTO shadow_equity_daily (date, equity, cash) VALUES"
        " ('2026-08-18', '0', '0'),"
        " ('2026-08-19', '300', '300')")
    conn.commit()
    monkeypatch.setattr(shadow_module, "today_et_date", lambda: "2026-08-20")
    history = ledger.get_equity_history(days=30)
    dates = [d.date().isoformat() for d, _ in history]
    assert dates == ["2026-08-19"]


# -- Minor 9: record_fill rewrites the order row's notional -------------------

def test_record_fill_rewrites_notional_for_notional_based_order(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(notional=Decimal(1000)))  # qty 10 @ 100
    ledger.record_fill(order.id, Decimal(90))
    row = conn.execute(
        "SELECT qty, notional, fill_price FROM shadow_orders WHERE id = ?",
        (int(order.id),)).fetchone()
    assert row["fill_price"] == "90"
    assert row["qty"] == "10.000000"  # filled qty unchanged (6dp-quantized at fill time)
    assert row["notional"] == "900.000000"  # 10.000000 * 90, not the original $1000 target


def test_record_fill_leaves_notional_null_for_qty_based_order(tmp_path):
    ledger, conn, _ = make_ledger(tmp_path, prices={"AAPL": Decimal(100)}, cash="10000")
    order = ledger.submit_order(buy(qty=Decimal(10)))
    ledger.record_fill(order.id, Decimal(90))
    row = conn.execute(
        "SELECT notional FROM shadow_orders WHERE id = ?", (int(order.id),)).fetchone()
    assert row["notional"] is None


# -- shadow-dual-active T4 review Important 3: quote-fetch amplification ----
# ShadowLedger already has exactly one shadow_positions row per ticker
# (PRIMARY KEY), so get_account()/get_positions() were never re-fetching a
# quote twice for the SAME ticker within one call -- the amplification came
# from cross-call/cross-account repetition (get_account + get_positions
# each re-valuing the same positions, every dashboard render, every
# sentinel tick, for both accounts sharing one process). That's fixed at
# the shared YFinanceSource cache (see tests/test_data.py); these tests
# confirm ShadowLedger's own per-call fetch count and that it actually
# benefits from that shared cache end-to-end.

class _CountingData(FakeData):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def get_quote(self, ticker: str) -> Quote:
        self.calls.append(ticker)
        return super().get_quote(ticker)


def test_get_account_fetches_each_distinct_ticker_quote_exactly_once(tmp_path):
    conn = connect(tmp_path / "t.db")
    data = _CountingData(prices={
        "AAPL": Decimal(100), "MSFT": Decimal(200), "TSLA": Decimal(300)})
    ledger = ShadowLedger(conn, data)
    ledger.set_cash(Decimal(100000))
    for ticker in ("AAPL", "MSFT", "TSLA"):
        ledger.submit_order(buy(ticker=ticker, qty=Decimal(1)))
    data.calls.clear()

    ledger.get_account()

    assert sorted(data.calls) == ["AAPL", "MSFT", "TSLA"]


def test_get_positions_fetches_each_distinct_ticker_quote_exactly_once(tmp_path):
    conn = connect(tmp_path / "t.db")
    data = _CountingData(prices={"AAPL": Decimal(100), "MSFT": Decimal(200)})
    ledger = ShadowLedger(conn, data)
    ledger.set_cash(Decimal(100000))
    for ticker in ("AAPL", "MSFT"):
        ledger.submit_order(buy(ticker=ticker, qty=Decimal(1)))
    data.calls.clear()

    ledger.get_positions()

    assert sorted(data.calls) == ["AAPL", "MSFT"]


class _StubFastInfoTicker:
    def __init__(self, price: Decimal) -> None:
        self.fast_info = {"last_price": price, "regular_market_previous_close": price}

    def history(self, period, interval="1d"):  # pragma: no cover — get_bars not exercised
        raise NotImplementedError


def test_shadow_ledger_get_account_reuses_the_shared_yfinance_quote_cache(tmp_path):
    """End-to-end with the REAL YFinanceSource (not the test FakeData
    above): repeated get_account()/get_positions() calls within the TTL
    must cost zero additional underlying Ticker fetches -- this is the
    actual fix for the measured 14-fetches-per-dual-tick amplification."""
    yf_module._quote_cache.clear()
    try:
        calls: list[str] = []

        def factory(ticker):
            calls.append(ticker)
            return _StubFastInfoTicker(Decimal(100))

        data = YFinanceSource(ticker_factory=factory)
        conn = connect(tmp_path / "t.db")
        ledger = ShadowLedger(conn, data)
        ledger.set_cash(Decimal(100000))
        ledger.submit_order(buy(ticker="AAPL", qty=Decimal(1)))
        calls.clear()

        ledger.get_account()
        ledger.get_account()
        ledger.get_positions()

        # submit_order's own quote lookup already warmed the cache for
        # AAPL above (before `calls.clear()`) -- every subsequent lookup
        # here, across three more calls spanning both get_account and
        # get_positions, is a pure cache hit costing zero further fetches.
        assert calls == []
    finally:
        yf_module._quote_cache.clear()
