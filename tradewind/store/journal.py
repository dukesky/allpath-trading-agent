from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from tradewind.broker.base import Order, OrderIntent
from tradewind.risk.gate import RiskDecision


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
            " strategy_id, risk_reasons, broker_order_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self._conn.commit()
        return cur.lastrowid

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
