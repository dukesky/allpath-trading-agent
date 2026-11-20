"""The end of the shadow account's study period.

The shadow account was reconciled with the owner's real brokerage account on
2026-11-20 and has been an advisory account since (holdings sync, option
holdings, Chinese cards, pre-trade consults). Its rows after that moment are
not part of the human-vs-agent study and are not comparable with the rows
before it. Every `allpath-research` export therefore drops shadow rows after
the cut by default (`--shadow-study-end`, default `DEFAULT_SHADOW_STUDY_END`;
`--shadow-study-end none` keeps everything). Paper rows are never touched.

A row is placed by its timestamp column when it has one, else by its ET
date; the cut's own ET day is kept in per-day tables (the cut fell after
that day's close)."""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from allpath_trade.research.db import ET

DEFAULT_SHADOW_STUDY_END = "2026-11-20T21:27:59Z"
CUT_ACCOUNT = "shadow"
_TS_KEYS = ("ts", "proposed_ts", "decision_ts", "decided_ts")
_DATE_KEYS = ("date_et", "date", "decision_date")


def parse_cut(value: str | None) -> datetime | None:
    """`--shadow-study-end`: an ISO timestamp (a bare date means the end of
    that UTC day), or none / off / "" for no cut. Raises ValueError on
    anything else."""
    text = (value or "").strip()
    if text.lower() in ("", "none", "off", "no"):
        return None
    if len(text) == 10:
        text += "T23:59:59+00:00"
    cut = datetime.fromisoformat(text)
    return cut if cut.tzinfo is not None else cut.replace(tzinfo=UTC)


def _moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def in_study(row: dict[str, Any], cut: datetime | None) -> bool:
    """False only for a shadow row dated after `cut`."""
    account = row.get("account")
    if account is None and str(row.get("purpose") or "").endswith(f":{CUT_ACCOUNT}"):
        account = CUT_ACCOUNT   # llm_daily.csv: usage labelled "reflection:shadow"
    if cut is None or account != CUT_ACCOUNT:
        return True
    for key in _TS_KEYS:
        if row.get(key):
            moment = _moment(row[key])
            if moment is not None:
                return moment <= cut
    for key in _DATE_KEYS:
        if row.get(key):
            try:
                return date.fromisoformat(str(row[key])[:10]) <= cut.astimezone(ET).date()
            except ValueError:
                continue
    return True


def apply(rows: list[dict[str, Any]], cut: datetime | None) -> tuple[list[dict[str, Any]], int]:
    """`(rows in the study, number of shadow rows dropped)`."""
    kept = [row for row in rows if in_study(row, cut)]
    return kept, len(rows) - len(kept)
