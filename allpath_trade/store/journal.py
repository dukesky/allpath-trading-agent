from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from allpath_trade.broker.base import Order, OrderIntent
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
            " strategy_id, risk_reasons, broker_order_id, filled_qty, filled_avg_price)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def refresh_fill(self, trade_id: int, order: Order) -> None:
        """Update a trade's fill columns + status from a re-fetched Order.

        Called once, right after submission, by Executor.execute -- see the
        comment there for why a single poll (not polling to completion) is
        the right amount of ceremony here."""
        self._conn.execute(
            "UPDATE trades SET filled_qty = ?, filled_avg_price = ?, status = ?"
            " WHERE id = ?",
            (
                str(order.filled_qty),
                str(order.filled_avg_price) if order.filled_avg_price is not None else None,
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
