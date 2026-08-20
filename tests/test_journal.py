import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal, is_recent_submission


def make_journal(tmp_path):
    return TradeJournal(connect(tmp_path / "t.db"))


INTENT = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal(500),
                     reason="dip buy", strategy_id="aapl-long")
ORDER = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
              notional=Decimal(500), status=OrderStatus.FILLED,
              filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
              submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC),
              filled_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))


def test_record_submitted_and_recent(tmp_path):
    j = make_journal(tmp_path)
    rid = j.record(INTENT, RiskDecision(approved=True), ORDER)
    assert rid == 1
    [row] = j.recent()
    assert row["ticker"] == "AAPL"
    assert row["status"] == "filled"
    assert row["broker_order_id"] == "o1"
    assert row["strategy_id"] == "aapl-long"


def test_record_rejected(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=False, reasons=["too big"]), None)
    [row] = j.recent()
    assert row["status"] == "rejected"
    assert "too big" in row["risk_reasons"]


def test_trades_today_counts_only_executed_today(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    j.record(INTENT, RiskDecision(approved=False, reasons=["x"]), None)  # rejected: not counted
    assert j.trades_today() == 1


def test_record_status_override_wins(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=False, reasons=["boom"]), None,
              status_override="error")
    [row] = j.recent()
    assert row["status"] == "error"
    assert "boom" in row["risk_reasons"]


def test_trades_today_excludes_error_status(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    j.record(INTENT, RiskDecision(approved=False, reasons=["data error: boom"]), None,
              status_override="error")  # errored: not counted
    assert j.trades_today() == 1


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    connect(path)
    connect(path)  # second connect must not fail


def test_record_persists_fill_details_from_order(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    [row] = j.recent()
    assert row["filled_qty"] == "2.5"
    assert row["filled_avg_price"] == "200"


def test_record_fill_details_are_null_without_order(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=False, reasons=["x"]), None)
    [row] = j.recent()
    assert row["filled_qty"] is None
    assert row["filled_avg_price"] is None


def test_refresh_fill_updates_row(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))
    trade_id = j.record(INTENT, RiskDecision(approved=True), submitted)
    [row] = j.recent()
    assert row["status"] == "submitted"
    assert row["filled_qty"] == "0"
    assert row["filled_avg_price"] is None

    filled = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                   submitted_at=submitted.submitted_at)
    j.refresh_fill(trade_id, filled)
    [row] = j.recent()
    assert row["status"] == "filled"
    assert row["filled_qty"] == "2.5"
    assert row["filled_avg_price"] == "200"


def test_record_persists_filled_at_from_order(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    [row] = j.recent()
    assert row["filled_at"] == "2026-07-30T15:00:00+00:00"


def test_record_filled_at_is_null_without_a_fill(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))
    j.record(INTENT, RiskDecision(approved=True), submitted)
    [row] = j.recent()
    assert row["filled_at"] is None


def test_refresh_fill_writes_filled_at(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 8, 9, 20, 27, tzinfo=UTC))
    trade_id = j.record(INTENT, RiskDecision(approved=True), submitted)

    filled = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal(1), filled_avg_price=Decimal("332.01"),
                   submitted_at=submitted.submitted_at,
                   filled_at=datetime(2026, 8, 10, 13, 34, tzinfo=UTC))
    j.refresh_fill(trade_id, filled)
    [row] = j.recent()
    assert row["filled_at"] == "2026-08-10T13:34:00+00:00"


def test_refresh_fill_never_regresses_an_already_filled_row(tmp_path):
    # Task 1 P6 review: a stale/out-of-order poll must not downgrade a row
    # that already reads FILLED back to submitted or wipe its fill data.
    j = make_journal(tmp_path)
    trade_id = j.record(INTENT, RiskDecision(approved=True), ORDER)  # already filled

    stale = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                  notional=Decimal(500), status=OrderStatus.SUBMITTED,
                  filled_qty=Decimal(0), filled_avg_price=None,
                  submitted_at=ORDER.submitted_at)
    j.refresh_fill(trade_id, stale)
    [row] = j.recent()
    assert row["status"] == "filled"
    assert row["filled_qty"] == "2.5"
    assert row["filled_avg_price"] == "200"


def test_unfilled_recent_returns_only_submitted_rows_with_a_broker_order_id(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime.now(UTC))
    j.record(INTENT, RiskDecision(approved=True), submitted)  # submitted, unfilled
    j.record(INTENT, RiskDecision(approved=True), ORDER)      # already filled
    j.record(INTENT, RiskDecision(approved=False, reasons=["x"]), None)  # rejected

    rows = j.unfilled_recent()
    assert len(rows) == 1
    assert rows[0]["broker_order_id"] == "o1"
    assert rows[0]["status"] == "submitted"


def test_unfilled_recent_includes_rows_far_outside_the_old_window(tmp_path):
    # I2: unfilled_recent USED to have a hard age cutoff (hours=48), which
    # meant a row old enough to fall outside it could never be selected
    # again -- an expired DAY order from days ago stayed 'submitted'
    # forever, with no path to ever learn its true terminal status. The
    # selection now has no age cutoff at all; only rendering (agent/
    # readonly_tools._format_recent_trade, dashboard.html) is age-gated.
    j = make_journal(tmp_path)
    old_ts = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    j._conn.execute(
        "INSERT INTO trades (ts, ticker, side, qty, notional, status, reason,"
        " strategy_id, risk_reasons, broker_order_id, filled_qty, filled_avg_price)"
        " VALUES (?, 'AAPL', 'buy', '1', NULL, 'submitted', 'old', NULL, '[]',"
        " 'o-old', '0', NULL)",
        (old_ts,))
    j._conn.commit()

    rows = j.unfilled_recent()
    assert len(rows) == 1
    assert rows[0]["broker_order_id"] == "o-old"


def test_unfilled_recent_includes_partially_filled_rows(tmp_path):
    # I3: a partial fill must keep getting re-polled until it reaches a
    # terminal status, not drop out of the sweep the moment the first
    # partial fill lands.
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.PARTIALLY_FILLED,
                      filled_qty=Decimal(1), filled_avg_price=Decimal(200),
                      submitted_at=datetime.now(UTC))
    j.record(INTENT, RiskDecision(approved=True), submitted)

    rows = j.unfilled_recent()
    assert len(rows) == 1
    assert rows[0]["status"] == "partially_filled"


def test_unfilled_recent_orders_by_id_desc_and_respects_limit(tmp_path):
    # I2 (M3): DESC + a SQL-level LIMIT, not an ASC scan sliced in Python --
    # the old ASC-plus-Python-slice combination meant the oldest N stuck
    # rows were re-selected on every single pass, head-of-line-blocking
    # every newer row behind them forever. DESC + LIMIT means a backlog
    # drains from the front: as the newest rows resolve and leave the
    # unresolved set, older rows rotate into the window.
    j = make_journal(tmp_path)
    submitted_at = datetime.now(UTC)
    ids = []
    for i in range(5):
        submitted = Order(id=f"o{i}", ticker="AAPL", side=OrderSide.BUY, qty=None,
                          notional=Decimal(500), status=OrderStatus.SUBMITTED,
                          filled_qty=Decimal(0), filled_avg_price=None,
                          submitted_at=submitted_at)
        ids.append(j.record(INTENT, RiskDecision(approved=True), submitted))

    rows = j.unfilled_recent(limit=3)
    assert [r["id"] for r in rows] == list(reversed(ids))[:3]


def test_is_recent_submission_true_within_the_window(tmp_path):
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    ts = (now - timedelta(hours=1)).isoformat()
    assert is_recent_submission(ts, now=now) is True


def test_is_recent_submission_false_outside_the_window(tmp_path):
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    ts = (now - timedelta(hours=72)).isoformat()
    assert is_recent_submission(ts, now=now) is False


def test_refresh_fill_writes_terminal_canceled_status_with_null_fills(tmp_path):
    # I2: a broker-reported terminal non-fill status (canceled/expired --
    # AlpacaBroker collapses "expired" onto CANCELED, see broker/alpaca.py)
    # must be written like any other status update, with the fill columns
    # staying NULL rather than fabricated.
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 8, 2, 14, 0, tzinfo=UTC))
    trade_id = j.record(INTENT, RiskDecision(approved=True), submitted)

    expired = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                    notional=Decimal(500), status=OrderStatus.CANCELED,
                    filled_qty=Decimal(0), filled_avg_price=None,
                    submitted_at=submitted.submitted_at)
    j.refresh_fill(trade_id, expired)

    [row] = j.recent()
    assert row["status"] == "canceled"
    assert row["filled_qty"] == "0"
    assert row["filled_avg_price"] is None
    # Converged: no longer in the still-unresolved set.
    assert j.unfilled_recent() == []


# --- shadow-dual-active T1: account scoping -------------------------------

def test_two_account_interleave_isolated(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper = TradeJournal(conn)
    shadow = TradeJournal(conn, account="shadow")

    paper.record(INTENT, RiskDecision(approved=True), ORDER)
    shadow_intent = OrderIntent(ticker="MSFT", side=OrderSide.BUY, notional=Decimal(300),
                                reason="shadow buy", strategy_id="msft-long")
    shadow_order = Order(id="o2", ticker="MSFT", side=OrderSide.BUY, qty=None,
                         notional=Decimal(300), status=OrderStatus.FILLED,
                         filled_qty=Decimal(1), filled_avg_price=Decimal(300),
                         submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC),
                         filled_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))
    shadow.record(shadow_intent, RiskDecision(approved=True), shadow_order)

    [prow] = paper.recent()
    assert prow["ticker"] == "AAPL"
    assert prow["account"] == "paper"
    [srow] = shadow.recent()
    assert srow["ticker"] == "MSFT"
    assert srow["account"] == "shadow"

    assert paper.trades_today() == 1
    assert shadow.trades_today() == 1


def test_refresh_fill_does_not_cross_accounts(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper = TradeJournal(conn)
    shadow = TradeJournal(conn, account="shadow")
    trade_id = paper.record(INTENT, RiskDecision(approved=True), Order(
        id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None, notional=Decimal(500),
        status=OrderStatus.SUBMITTED, filled_qty=Decimal(0), filled_avg_price=None,
        submitted_at=datetime(2026, 8, 2, 14, 0, tzinfo=UTC)))

    filled = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                   submitted_at=datetime(2026, 8, 2, 14, 0, tzinfo=UTC),
                   filled_at=datetime(2026, 8, 2, 14, 5, tzinfo=UTC))
    # shadow's journal instance must not be able to touch paper's row even
    # if handed its id.
    shadow.refresh_fill(trade_id, filled)
    [prow] = paper.recent()
    assert prow["status"] == "submitted"

    paper.refresh_fill(trade_id, filled)
    [prow] = paper.recent()
    assert prow["status"] == "filled"


def test_legacy_trades_row_defaults_account_paper_after_migration(tmp_path):
    # Simulate a pre-shadow-dual-active database: `trades` exists without an
    # `account` column. CREATE TABLE IF NOT EXISTS won't touch an existing
    # table, so the ALTER TABLE migration must add + backfill it.
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,"
        " qty TEXT, notional TEXT, status TEXT NOT NULL, reason TEXT NOT NULL,"
        " strategy_id TEXT, risk_reasons TEXT NOT NULL DEFAULT '[]',"
        " broker_order_id TEXT)")
    raw.execute(
        "INSERT INTO trades (ts, ticker, side, status, reason)"
        " VALUES ('2020-01-01T00:00:00+00:00', 'AAPL', 'buy', 'filled', 'legacy')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT account FROM trades").fetchone()
    assert row["account"] == "paper"

    j = TradeJournal(conn)
    [row] = j.recent()
    assert row["account"] == "paper"
