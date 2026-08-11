from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from allpath_trade.broker.base import Order, OrderIntent, OrderStatus
from allpath_trade.risk.gate import RiskDecision


class TradeJournal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, intent: OrderIntent, decision: RiskDecision,
               order: Order | None, status_override: str | None = None) -> int:
        if status_override is not None:
            status = status_override
        else:
            status = order.status.value if (decision.approved and order) else "rejected"
        cur = self._conn.execute(
            "INSERT INTO trades (ts, ticker, side, qty, notional, status, reason,"
            " strategy_id, risk_reasons, broker_order_id, filled_qty, filled_avg_price,"
            " filled_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
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
        FILLED read can update it further (e.g. refining filled_avg_price)."""
        current = self._conn.execute(
            "SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if (current is not None and current["status"] == OrderStatus.FILLED.value
                and order.status != OrderStatus.FILLED):
            return
        self._conn.execute(
            "UPDATE trades SET filled_qty = ?, filled_avg_price = ?, filled_at = ?,"
            " status = ? WHERE id = ?",
            (
                str(order.filled_qty),
                str(order.filled_avg_price) if order.filled_avg_price is not None else None,
                order.filled_at.isoformat() if order.filled_at is not None else None,
                order.status.value,
                trade_id,
            ),
        )
        self._conn.commit()

    def trades_today(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        day = now.date().isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE ts LIKE ?"
            " AND status NOT IN ('rejected', 'error')",
            (f"{day}%",),
        ).fetchone()
        return int(row["n"])

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)))

    def unfilled_recent(self, hours: int = 48) -> list[sqlite3.Row]:
        """Rows still awaiting a fill: broker_order_id present, status still
        'submitted', submitted within the last `hours`. Feeds
        execution.refresh_pending_fills -- the ongoing sweep that catches
        fills the single post-submit poll in Executor.execute missed (e.g.
        a DAY order queued outside market hours that fills at the next
        open). The window bounds the query to genuinely recent activity
        rather than re-polling every submitted-and-never-resolved row ever
        journaled."""
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        return list(self._conn.execute(
            "SELECT * FROM trades WHERE status = 'submitted'"
            " AND broker_order_id IS NOT NULL AND ts >= ? ORDER BY id ASC",
            (since,)))
