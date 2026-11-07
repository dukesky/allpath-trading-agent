from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def open_readonly(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite file read-only (URI `mode=ro`, plus `query_only`), so
    no statement this package runs can change it. Safe on the live
    production DB (WAL readers never block the writer) and on a copy."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def et_date(ts: str | None) -> str | None:
    """The US/Eastern calendar date of a stored UTC ISO timestamp -- the same
    day convention the reports table uses (`reports.date` is the ET date)."""
    if not ts:
        return None
    return parse_ts(ts).astimezone(ET).date().isoformat()


def minutes_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return round((parse_ts(end) - parse_ts(start)).total_seconds() / 60, 2)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?",
                        (name,)).fetchone() is not None
