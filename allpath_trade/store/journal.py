from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from allpath_trade.broker.base import Order, OrderIntent, OrderStatus
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.store.accounts import DEFAULT_ACCOUNT

# Fill-honesty round (I2): how long a still-'submitted' row is presented as
# "fill pending" before rendering degrades to "status unconfirmed" instead.
# This is a RENDERING threshold only -- unfilled_recent's SELECTION lost its
# age cutoff in this same round (see below), so a row far outside this
# window is still actively polled by the sweep; it just isn't allowed to
# make an affirmative "pending" claim once staleness makes that claim
# untrustworthy. Lives here (not in agent/readonly_tools.py or web/format.py)
# because every trade-rendering surface -- agent/readonly_tools.py's
# _format_recent_trade, cli.py, memory/consolidate.py, and dashboard.html
# via web/format.py's Jinja filter -- already depends on this module for
# TradeJournal, and a shared constant/helper here can't drift out of sync
# between them the way four independent copies could.
FILL_PENDING_WINDOW_HOURS = 48


def is_recent_submission(ts: str, *, now: datetime | None = None,
                         hours: int = FILL_PENDING_WINDOW_HOURS) -> bool:
    """True when `ts` (a trade row's submission timestamp) is within `hours`
    of `now` (defaults to the real current time). `now` is a keyword param,
    not read from a default argument, so callers/tests can pin it without
    monkeypatching the clock."""
    now = now or datetime.now(UTC)
    then = datetime.fromisoformat(ts)
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return now - then < timedelta(hours=hours)


class TradeJournal:
    def __init__(self, conn: sqlite3.Connection, account: str = DEFAULT_ACCOUNT) -> None:
        self._conn = conn
        self._account = account

    def record(self, intent: OrderIntent, decision: RiskDecision,
               order: Order | None, status_override: str | None = None) -> int:
        if status_override is not None:
            status = status_override
        else:
            status = order.status.value if (decision.approved and order) else "rejected"
        cur = self._conn.execute(
            "INSERT INTO trades (account, ts, ticker, side, qty, notional, status,"
            " reason, strategy_id, risk_reasons, broker_order_id, filled_qty,"
            " filled_avg_price, filled_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._account,
                datetime.now(UTC).isoformat(),
                intent.ticker,
                intent.side.value,
                str(intent.qty) if intent.qty is not None else None,
                str(intent.notional) if intent.notional is not None else None,
                status,
                intent.reason,
                intent.strategy_id,
                json.dumps(decision.reasons),
                order.id if order else None,
                str(order.filled_qty) if order else None,
                (str(order.filled_avg_price)
                 if order and order.filled_avg_price is not None else None),
                (order.filled_at.isoformat()
                 if order and order.filled_at is not None else None),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def refresh_fill(self, trade_id: int, order: Order) -> None:
        """Update a trade's fill columns + status from a re-fetched Order.

        Called both by Executor.execute's single post-submit poll and by the
        ongoing refresh_pending_fills sweep (execution.py) that re-checks
        older still-submitted rows on every sentinel pass.

        Guards against regressing a row that is already FILLED: the Task 1
        P6 review flagged that an unguarded overwrite lets a stale or
        out-of-order poll (e.g. a broker read replica lagging behind the
        fill it already reported) silently downgrade a filled row back to
        submitted. Once a row reads FILLED in the journal, only another
        FILLED read can update it further (e.g. refining filled_avg_price).

        Also converges a row onto a broker-reported terminal non-fill status
        (canceled/expired/rejected -- AlpacaBroker._to_order already
        collapses "expired" onto CANCELED, see broker/alpaca.py's
        _STATUS_MAP) exactly like any other status: the UPDATE below writes
        whatever `order.status`/filled_qty/filled_avg_price come back, and
        Order's own defaults (filled_qty="0", filled_avg_price=None on a
        broker response with no fill) mean a terminal non-fill status lands
        with its fill columns staying NULL, not fabricated. This is what
        lets a DAY order that expired at the broker long ago stop
        permanently claiming "fill pending" once the sweep (unfilled_recent,
        which lost its own age cutoff this round) finally reaches it.

        The SELECT-then-UPDATE runs inside one `transaction()`: without it,
        another thread's write could land between the guard's SELECT and
        this UPDATE, and the guard would be checking a status that's already
        stale by the time the UPDATE executes."""
        with self._conn.transaction():
            current = self._conn.execute(
                "SELECT status FROM trades WHERE id = ? AND account = ?",
                (trade_id, self._account)).fetchone()
            if (current is not None and current["status"] == OrderStatus.FILLED.value
                    and order.status != OrderStatus.FILLED):
                return
            self._conn.execute(
                "UPDATE trades SET filled_qty = ?, filled_avg_price = ?, filled_at = ?,"
                " status = ? WHERE id = ? AND account = ?",
                (
                    str(order.filled_qty),
                    str(order.filled_avg_price) if order.filled_avg_price is not None else None,
                    order.filled_at.isoformat() if order.filled_at is not None else None,
                    order.status.value,
                    trade_id,
                    self._account,
                ),
            )

    def trades_today(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        day = now.date().isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE ts LIKE ? AND account = ?"
            " AND status NOT IN ('rejected', 'error')",
            (f"{day}%", self._account),
        ).fetchone()
        return int(row["n"])

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM trades WHERE account = ? ORDER BY id DESC LIMIT ?",
            (self._account, limit)))

    def unfilled_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        """Rows still awaiting a terminal broker answer: broker_order_id
        present, status still 'submitted' or 'partially_filled'. Feeds
        execution.refresh_pending_fills -- the ongoing sweep that catches
        fills the single post-submit poll in Executor.execute missed (e.g.
        a DAY order queued outside market hours that fills at the next
        open), and now also catches rows that expired/canceled at the
        broker without ever filling (see refresh_fill's docstring).

        Fill-honesty round (I2): deliberately has NO age cutoff anymore. The
        old `hours`-windowed WHERE clause meant a row old enough to fall
        outside the window could never be selected again -- an expired DAY
        order from days ago stayed 'submitted' in the journal forever, with
        no path to ever learn its true terminal status. Every unresolved
        row is now eligible for every sweep.

        What bounds the query is `ORDER BY id DESC LIMIT ?` instead: each
        pass polls the `limit` MOST RECENT unresolved rows. This also fixes
        the old head-of-line blocking (M3): the previous `ORDER BY id ASC`
        plus a Python-side `[:20]` slice meant the oldest 20 stuck rows were
        polled on *every single pass* while newer rows waited behind them
        indefinitely. DESC means a backlog drains from the front: once the
        newest unresolved rows resolve (fill or terminal non-fill status)
        and drop out of this query's result set entirely, the next-newest
        rows -- eventually including the old stuck ones -- rotate into the
        LIMIT window. `partially_filled` joins `submitted` in the WHERE so a
        partial fill keeps getting re-polled until it reaches a terminal
        status too, instead of quietly falling out of the sweep the moment
        the first partial fill lands (I3)."""
        return list(self._conn.execute(
            "SELECT * FROM trades WHERE account = ?"
            " AND status IN ('submitted', 'partially_filled')"
            " AND broker_order_id IS NOT NULL ORDER BY id DESC LIMIT ?",
            (self._account, limit)))
