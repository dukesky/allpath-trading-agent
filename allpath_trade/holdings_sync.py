"""Holdings sync (HOLDINGS_SYNC / SHADOW_HOLDINGS_SYNC): make the shadow
ledger equal the owner's real brokerage account.

The app has no connection to the brokerage and never logs in to it. The
holdings arrive only as something the owner sends: a screenshot of the
positions screen (read by the chat model, which passes what it read as
structured rows), a CSV export, or a typed list ("NVDA 40 @ 182.5, cash
12000"). All three end in the same `ParsedHoldings`.

Nothing is applied silently. The chat tool `propose_holdings_sync` builds
the diff against the current ledger (new, closed, quantity or cost changed,
cash) and queues ONE `shadow_edit` review (op `sync`) whose card shows the
parsed table and the diff; the ledger changes only when the owner confirms
it. A row that could not be read, a duplicate, or a new symbol with no cost
basis is asked about instead of guessed: nothing is queued until it is
resolved. Applying writes one row to `shadow_syncs` (when, by whom, source,
before, after), so a reconciliation can always be told apart from a trade,
and the research exports can mark where "ledger = real account" begins.

Strategies and rule states are not touched: a symbol that is still held
keeps its strategy and stops; a held symbol with no active strategy is
listed as "no plan".

With the flag off the tool is not registered and nothing here runs.
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from allpath_trade import i18n
from allpath_trade.i18n import tr

if TYPE_CHECKING:
    from allpath_trade.agent.tools import ToolRegistry

FLAG = "holdings_sync"
OP = "sync"
SOURCES = ("screenshot", "csv", "text")
MODES = ("full", "partial")
MAX_ROWS = 500

HOLDINGS_SYNC_CHAT_NOTE = """\

Holdings sync. This account's ledger is meant to equal the owner's REAL
brokerage account. When he sends a screenshot of his positions, a CSV export
or a typed list of holdings and wants the ledger brought in line with it,
call propose_holdings_sync ONCE with everything (not shadow_set_position
row by row):
- From a screenshot: pass `positions` with exactly what you can read
  (ticker, quantity, average cost when shown) and `cash` when shown. Put any
  row or number you cannot read with certainty in `unreadable` (one short
  line each, e.g. "3rd row: quantity cut off"). Never fill in a guess. The
  market value or the current price is NOT the average cost; leave
  `avg_cost` out when the screen does not show it.
- From pasted text or a file: pass it verbatim as `text`.
- `complete`: true only when the input shows his WHOLE account. If the
  list may be cut off (a scrolled screen, "top holdings"), pass false: then
  nothing is closed, only the listed symbols are updated.
The tool never changes the ledger. It answers with questions when something
is unclear (ask him those and call it again with his answers), or with the
comparison it queued for his confirmation. Show him that comparison as it
is; do not re-compute it. Tell him nothing changes until he confirms on the
card. You never log in to his brokerage and never ask for its password.
"""


@dataclass
class ParsedHoldings:
    """What any input path produced. `rows` hold `ticker`, `qty` and
    `avg_cost` (None when the input gave no cost) as strings; `problems`
    are things to ask the owner about, in plain words."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    cash: Decimal | None = None
    problems: list[str] = field(default_factory=list)


# --- parsing --------------------------------------------------------------

_NUM = r"\$?\s*-?\d[\d,]*(?:\.\d+)?|\$?\s*-?\.\d+"
_CASH_WORDS = ("cash", "现金", "buying power", "可用资金")
_ENTRY = re.compile(
    rf"^(?P<ticker>[A-Za-z][A-Za-z0-9.\-]{{0,9}})\s*[:：]?\s+(?P<qty>{_NUM})\s*"
    r"(?:shares?|sh|股)?\s*"
    rf"(?:(?:@|at|成本|均价|avg(?:\s*cost)?)?\s*(?P<cost>{_NUM}))?\s*$", re.IGNORECASE)
_CASH = re.compile(
    rf"^(?:{'|'.join(_CASH_WORDS)})\s*[:：=]?\s*(?P<amount>{_NUM})\s*$", re.IGNORECASE)
# A comma separates entries when a space or a letter follows it; "12,000"
# (a digit right after the comma) stays one number.
_SPLIT = re.compile(r"[;；\n]|[,，](?=\s)|[,，](?=[A-Za-z一-鿿])")
_HEADER_TICKER = ("ticker", "symbol", "instrument", "代码", "股票")
_HEADER_QTY = ("qty", "quantity", "shares", "数量", "股数")
_HEADER_COST = ("avg_cost", "average cost", "avg cost", "average_cost", "cost basis",
                "cost", "成本", "均价")


def _number(raw: Any, name: str) -> Decimal:
    from allpath_trade.agent.shadow_tools import _parse_money

    text = str(raw).strip().replace("$", "").replace(",", "").replace(" ", "")
    return _parse_money(text, field=name)


def _add_row(out: ParsedHoldings, seen: set[str], ticker_raw: Any, qty_raw: Any,
             cost_raw: Any, where: str) -> None:
    from allpath_trade.agent.shadow_tools import _valid_ticker

    ticker = _valid_ticker(str(ticker_raw or ""))
    if ticker is None:
        out.problems.append(f"{where}: {str(ticker_raw)!r} is not a ticker symbol")
        return
    if ticker in seen:
        out.problems.append(f"{where}: {ticker} appears more than once")
        return
    try:
        qty = _number(qty_raw, "quantity")
    except ValueError:
        out.problems.append(f"{where}: cannot read the quantity of {ticker} ({qty_raw!r})")
        return
    if qty < 0:
        out.problems.append(f"{where}: {ticker} has a negative quantity ({qty})")
        return
    cost: Decimal | None = None
    if cost_raw not in (None, ""):
        try:
            cost = _number(cost_raw, "average cost")
        except ValueError:
            out.problems.append(
                f"{where}: cannot read the average cost of {ticker} ({cost_raw!r})")
            return
        if cost <= 0:
            out.problems.append(f"{where}: {ticker} has an average cost of {cost}")
            return
    seen.add(ticker)
    out.rows.append({"ticker": ticker, "qty": str(qty),
                     "avg_cost": None if cost is None else str(cost)})


def _set_cash(out: ParsedHoldings, raw: Any, where: str) -> None:
    if out.cash is not None:
        out.problems.append(f"{where}: cash is given more than once")
        return
    try:
        amount = _number(raw, "cash")
    except ValueError:
        out.problems.append(f"{where}: cannot read the cash amount ({raw!r})")
        return
    if amount < 0:
        out.problems.append(f"{where}: cash is negative ({amount}); is margin in use?")
        return
    out.cash = amount


def _header_columns(cells: list[str]) -> dict[str, int] | None:
    """Column positions when `cells` is a header row naming at least a
    ticker and a quantity column."""
    lowered = [c.strip().lower() for c in cells]
    found: dict[str, int] = {}
    for name, words in (("ticker", _HEADER_TICKER), ("qty", _HEADER_QTY),
                        ("cost", _HEADER_COST)):
        for index, cell in enumerate(lowered):
            if index in found.values():
                continue
            if any(cell == w or cell.startswith(w) for w in words):
                found[name] = index
                break
    return found if "ticker" in found and "qty" in found else None


def parse_holdings_text(text: str) -> ParsedHoldings:
    """A CSV export or a typed list. Understands a header row (symbol /
    quantity / average cost in any order), bare `TICKER,QTY,COST` rows,
    `NVDA 40 @ 182.5`, `NVDA 40 shares at $182.50`, `NVDA 40` (no cost) and
    `cash 12000` / `CASH,12000`; entries may be separated by new lines,
    semicolons or commas. Anything else becomes a problem to ask about."""
    out = ParsedHoldings()
    seen: set[str] = set()
    text = (text or "").replace("﻿", "")
    if len(text) > 200_000:
        out.problems.append("the text is too long to be a holdings list")
        return out
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    columns: dict[str, int] | None = None
    entry_no = 0
    for line in lines:
        cells = [c.strip() for c in next(csv.reader(io.StringIO(line)), [])]
        if columns is None and entry_no == 0 and _header_columns(cells):
            columns = _header_columns(cells)
            continue
        if columns is not None:
            entry_no += 1
            where = f"row {entry_no}"
            if not any(cells):
                continue
            first = cells[columns["ticker"]] if columns["ticker"] < len(cells) else ""
            if first.lower() in _CASH_WORDS:
                rest = [c for i, c in enumerate(cells) if i != columns["ticker"] and c]
                _set_cash(out, rest[0] if rest else "", where)
                continue
            qty = cells[columns["qty"]] if columns["qty"] < len(cells) else ""
            cost = (cells[columns["cost"]] if "cost" in columns
                    and columns["cost"] < len(cells) else None)
            _add_row(out, seen, first, qty, cost, where)
            continue
        # A bare CSV row: TICKER,QTY[,COST] or CASH,AMOUNT.
        if len(cells) in (2, 3) and re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,9}|现金", cells[0]) \
                and re.fullmatch(_NUM, cells[1] or "x"):
            entry_no += 1
            where = f"entry {entry_no}"
            if cells[0].lower() in _CASH_WORDS:
                _set_cash(out, cells[1], where)
            else:
                _add_row(out, seen, cells[0], cells[1],
                         cells[2] if len(cells) == 3 else None, where)
            continue
        for piece in _SPLIT.split(line):
            piece = piece.strip().strip(",，.。")
            if not piece:
                continue
            entry_no += 1
            where = f"entry {entry_no}"
            cash = _CASH.match(piece)
            if cash:
                _set_cash(out, cash.group("amount"), where)
                continue
            entry = _ENTRY.match(piece)
            if entry is None:
                out.problems.append(f"{where}: could not read {piece[:60]!r}")
                continue
            _add_row(out, seen, entry.group("ticker"), entry.group("qty"),
                     entry.group("cost"), where)
        if len(out.rows) > MAX_ROWS:
            out.problems.append(f"more than {MAX_ROWS} positions")
            break
    if not out.rows and out.cash is None and not out.problems:
        out.problems.append("no holdings found in the text")
    return out


def parse_extraction(positions: Any, cash: Any = None,
                     unreadable: Any = None) -> ParsedHoldings:
    """What the vision model read off a screenshot: `positions` is a list of
    `{ticker, qty, avg_cost?}`, `unreadable` its own list of rows it could
    not read. Every value is validated here; nothing is repaired."""
    out = ParsedHoldings()
    seen: set[str] = set()
    for note in unreadable if isinstance(unreadable, list) else []:
        text = " ".join(str(note).split())
        if text:
            out.problems.append(f"unreadable in the image: {text[:160]}")
    if not isinstance(positions, list):
        if positions not in (None, ""):
            out.problems.append("positions must be a list of rows")
        positions = []
    for index, row in enumerate(positions[:MAX_ROWS + 1], 1):
        where = f"row {index}"
        if not isinstance(row, dict):
            out.problems.append(f"{where}: not a position row")
            continue
        qty = row.get("qty", row.get("quantity", row.get("shares")))
        if qty in (None, ""):
            out.problems.append(f"{where}: no quantity for {row.get('ticker')!r}")
            continue
        _add_row(out, seen, row.get("ticker", row.get("symbol")), qty,
                 row.get("avg_cost", row.get("average_cost")), where)
    if len(positions) > MAX_ROWS:
        out.problems.append(f"more than {MAX_ROWS} positions")
    if cash not in (None, ""):
        _set_cash(out, cash, "cash")
    if not out.rows and out.cash is None and not out.problems:
        out.problems.append("no holdings were read")
    return out


# --- diff -----------------------------------------------------------------

@dataclass
class SyncDiff:
    """The comparison of parsed holdings with the ledger. Every list holds
    plain dicts so the whole thing can be stored on the review."""

    mode: str
    new: list[dict[str, str]] = field(default_factory=list)
    closed: list[dict[str, str]] = field(default_factory=list)
    qty_changed: list[dict[str, str]] = field(default_factory=list)
    cost_changed: list[dict[str, str]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    not_listed: list[str] = field(default_factory=list)   # partial mode: left alone
    cost_kept: list[str] = field(default_factory=list)    # no cost given: old cost kept
    cash_before: str = "0"
    cash_after: str | None = None                         # None = cash not in the input
    no_plan: list[str] = field(default_factory=list)
    needs_cost: list[str] = field(default_factory=list)   # new symbol without a cost
    after: dict[str, Any] = field(default_factory=dict)   # full_snapshot shape
    rows: list[dict[str, str]] = field(default_factory=list)  # what applying will set

    @property
    def cash_changed(self) -> bool:
        return self.cash_after is not None and Decimal(self.cash_after) != Decimal(
            self.cash_before)

    @property
    def changes(self) -> int:
        return (len(self.new) + len(self.closed) + len(self.qty_changed)
                + len(self.cost_changed) + int(self.cash_changed))

    def counts(self) -> dict[str, int]:
        return {"new": len(self.new), "closed": len(self.closed),
                "qty_changed": len(self.qty_changed),
                "cost_changed": len(self.cost_changed),
                "cash_changed": int(self.cash_changed),
                "unchanged": len(self.unchanged)}


def diff_holdings(before: dict[str, Any], parsed: ParsedHoldings, *, mode: str = "full",
                  planned: set[str] | None = None) -> SyncDiff:
    """`before` is `shadow_tools.full_snapshot`'s dict. `planned` is the set
    of tickers that have an active strategy; None leaves `no_plan` empty."""
    diff = SyncDiff(mode=mode, cash_before=str(before.get("cash", "0")))
    old = {t: dict(p) for t, p in (before.get("positions") or {}).items()}
    after_positions = {} if mode == "full" else {t: dict(p) for t, p in old.items()}
    listed = set()
    for row in parsed.rows:
        ticker, qty = row["ticker"], Decimal(row["qty"])
        listed.add(ticker)
        held = old.get(ticker)
        cost = row.get("avg_cost")
        if qty == 0:
            if held is not None:
                diff.closed.append({"ticker": ticker, **held})
                after_positions.pop(ticker, None)
            continue
        if held is None:
            if cost is None:
                diff.needs_cost.append(ticker)
                continue
            diff.new.append({"ticker": ticker, "qty": row["qty"], "avg_cost": cost})
        else:
            if cost is None:
                cost = held["avg_cost"]
                diff.cost_kept.append(ticker)
            qty_moved = Decimal(held["qty"]) != qty
            cost_moved = Decimal(held["avg_cost"]) != Decimal(cost)
            entry = {"ticker": ticker, "old_qty": held["qty"], "qty": row["qty"],
                     "old_avg_cost": held["avg_cost"], "avg_cost": cost}
            if qty_moved:
                diff.qty_changed.append(entry)
            elif cost_moved:
                diff.cost_changed.append(entry)
            else:
                diff.unchanged.append(ticker)
                after_positions[ticker] = dict(held)
                continue
        after_positions[ticker] = {"qty": row["qty"], "avg_cost": cost}
        diff.rows.append({"ticker": ticker, "qty": row["qty"], "avg_cost": cost})
    for ticker, held in old.items():
        if ticker in listed:
            continue
        if mode == "full":
            diff.closed.append({"ticker": ticker, **held})
        else:
            diff.not_listed.append(ticker)
    if parsed.cash is not None:
        diff.cash_after = str(parsed.cash)
    diff.after = {"positions": after_positions,
                  "cash": diff.cash_after if diff.cash_after is not None
                  else diff.cash_before}
    if planned is not None:
        diff.no_plan = sorted(t for t in after_positions if t not in planned)
    for key in ("new", "closed", "qty_changed", "cost_changed"):
        getattr(diff, key).sort(key=lambda e: e["ticker"])
    return diff


# --- text -----------------------------------------------------------------

i18n.register({
    "Holdings sync ({source}, {mode})": "持仓同步（{source}，{mode}）",
    "screenshot": "截图", "csv": "CSV", "text": "文字",
    "whole account": "完整账户", "listed symbols only": "只更新列出的",
    "New ({n}):": "新增（{n}）：",
    "Closed, not in the input ({n}):": "清仓，输入里没有（{n}）：",
    "Closed ({n}):": "清仓（{n}）：",
    "Quantity changed ({n}):": "数量变化（{n}）：",
    "Cost changed ({n}):": "成本变化（{n}）：",
    "  {ticker}  {qty} sh @ {cost}": "  {ticker}  {qty} 股 @ {cost}",
    "  {ticker}  was {qty} sh @ {cost}": "  {ticker}  原 {qty} 股 @ {cost}",
    "  {ticker}  {old_qty} → {qty} sh, cost {old_cost} → {cost}":
        "  {ticker}  {old_qty} → {qty} 股，成本 {old_cost} → {cost}",
    "  {ticker}  {old_qty} → {qty} sh, cost {cost} (kept)":
        "  {ticker}  {old_qty} → {qty} 股，成本 {cost}（沿用旧值）",
    "  {ticker}  {qty} sh, cost {old_cost} → {cost}":
        "  {ticker}  {qty} 股，成本 {old_cost} → {cost}",
    "Unchanged: {n}": "没变：{n} 只",
    "Not in the input, left as they are: {tickers}": "输入里没有，保持不变：{tickers}",
    "Cash: {old} → {new}": "现金：{old} → {new}",
    "Cash: {old} (unchanged)": "现金：{old}（没变）",
    "Cash: not in the input, stays {old}": "现金：输入里没有，保持 {old}",
    "No plan (held, no active strategy): {tickers}": "没有计划（有持仓但没有生效的策略）：{tickers}",
    "The ledger already matches. Confirming records the reconciliation.":
        "账本已经和输入一致。确认后会记录这次对账。",
    "Nothing changes until you confirm. Strategies and stops are not touched.":
        "你确认之前账本不会变。策略和止损不受影响。",
    "Holdings sync: {n} change(s)": "持仓同步：{n} 处变化",
    "Holdings sync: no change": "持仓同步：无变化",
    "{prefix}[AllPath] Holdings sync: confirm {n} change(s)":
        "{prefix}[AllPath] 持仓同步：请确认 {n} 处变化",
    "Your shadow ledger was last synced with your real account {days} days ago. "
    "Send a fresh screenshot of your positions (with cash) when you can.":
        "shadow 账本上次和真实账户对账是 {days} 天前。方便时请发一张最新的持仓截图（带现金）。",
    "Your shadow ledger has never been synced with your real account. "
    "Send a screenshot of your positions (with cash) to start.":
        "shadow 账本还没有和真实账户对过账。请发一张持仓截图（带现金）开始。",
})


def _money(value: Any) -> str:
    from allpath_trade.compact_cards import money

    return money(value)


def _qty(value: Any) -> str:
    number = Decimal(str(value))
    text = f"{number:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_diff(diff: SyncDiff, *, source: str, lang: str = "en") -> str:
    """The table the owner confirms: one block per kind of change."""
    def t(text: str, **kw: Any) -> str:
        return tr(lang, text, **kw)

    lines = [t("Holdings sync ({source}, {mode})", source=t(source),
               mode=t("whole account" if diff.mode == "full" else "listed symbols only"))]
    if diff.new:
        lines.append(t("New ({n}):", n=len(diff.new)))
        lines += [t("  {ticker}  {qty} sh @ {cost}", ticker=e["ticker"], qty=_qty(e["qty"]),
                    cost=_money(e["avg_cost"])) for e in diff.new]
    if diff.closed:
        lines.append(t("Closed, not in the input ({n}):" if diff.mode == "full"
                       else "Closed ({n}):", n=len(diff.closed)))
        lines += [t("  {ticker}  was {qty} sh @ {cost}", ticker=e["ticker"],
                    qty=_qty(e["qty"]), cost=_money(e["avg_cost"])) for e in diff.closed]
    if diff.qty_changed:
        lines.append(t("Quantity changed ({n}):", n=len(diff.qty_changed)))
        for e in diff.qty_changed:
            if e["ticker"] in diff.cost_kept:
                lines.append(t("  {ticker}  {old_qty} → {qty} sh, cost {cost} (kept)",
                               ticker=e["ticker"], old_qty=_qty(e["old_qty"]),
                               qty=_qty(e["qty"]), cost=_money(e["avg_cost"])))
            else:
                lines.append(t("  {ticker}  {old_qty} → {qty} sh, cost {old_cost} → {cost}",
                               ticker=e["ticker"], old_qty=_qty(e["old_qty"]),
                               qty=_qty(e["qty"]), old_cost=_money(e["old_avg_cost"]),
                               cost=_money(e["avg_cost"])))
    if diff.cost_changed:
        lines.append(t("Cost changed ({n}):", n=len(diff.cost_changed)))
        lines += [t("  {ticker}  {qty} sh, cost {old_cost} → {cost}", ticker=e["ticker"],
                    qty=_qty(e["qty"]), old_cost=_money(e["old_avg_cost"]),
                    cost=_money(e["avg_cost"])) for e in diff.cost_changed]
    lines.append(t("Unchanged: {n}", n=len(diff.unchanged)))
    if diff.not_listed:
        lines.append(t("Not in the input, left as they are: {tickers}",
                       tickers=", ".join(sorted(diff.not_listed))))
    if diff.cash_after is None:
        lines.append(t("Cash: not in the input, stays {old}", old=_money(diff.cash_before)))
    elif diff.cash_changed:
        lines.append(t("Cash: {old} → {new}", old=_money(diff.cash_before),
                       new=_money(diff.cash_after)))
    else:
        lines.append(t("Cash: {old} (unchanged)", old=_money(diff.cash_before)))
    if diff.no_plan:
        lines.append(t("No plan (held, no active strategy): {tickers}",
                       tickers=", ".join(diff.no_plan)))
    if diff.changes == 0:
        lines.append(t("The ledger already matches. Confirming records the reconciliation."))
    return "\n".join(lines)


# --- applying and the reconciliation record ---------------------------------

def record_sync(conn: Any, *, args: dict[str, Any], before: Any, after: Any,
                now: datetime | None = None) -> int:
    """One row in `shadow_syncs` for an applied sync. Called by the
    shadow-edit applier inside the same transaction as the ledger writes."""
    now = now or datetime.now(UTC)
    token = str(args.get("token") or "")
    review_id = None
    if token:
        row = conn.execute(
            "SELECT id FROM pending_reviews WHERE account = 'shadow' AND kind = 'shadow_edit'"
            " AND condition = ? AND snapshot LIKE ? ORDER BY id DESC LIMIT 1",
            (OP, f'%"token": "{token}"%')).fetchone()
        review_id = row["id"] if row is not None else None
    counts = args.get("counts") or {}
    cur = conn.execute(
        "INSERT INTO shadow_syncs (ts, account, source, mode, by, review_id, token,"
        " before, after, n_new, n_closed, n_qty_changed, n_cost_changed, cash_changed)"
        " VALUES (?, 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now.isoformat(), str(args.get("source") or ""), str(args.get("mode") or "full"),
         "owner", review_id, token, json.dumps(before, default=str),
         json.dumps(after, default=str), int(counts.get("new", 0)),
         int(counts.get("closed", 0)), int(counts.get("qty_changed", 0)),
         int(counts.get("cost_changed", 0)), int(counts.get("cash_changed", 0))))
    return int(cur.lastrowid)


def apply_sync(ledger: Any, conn: Any, args: dict[str, Any], before: Any) -> None:
    """The approved sync, in one transaction: close what is gone, set what
    is new or changed, set cash, write the reconciliation record. The caller
    (`apply_shadow_edit_factory`) has already checked staleness."""
    from allpath_trade.agent.shadow_tools import _parse_money, full_snapshot

    rows = [(row["ticker"], _parse_money(row["qty"], field="qty"),
             _parse_money(row["avg_cost"], field="avg_cost")) for row in args["rows"]]
    closed = [str(t) for t in args.get("closed", [])]
    cash = (_parse_money(args["cash"], field="cash")
            if args.get("cash") is not None else None)
    with conn.transaction():
        for ticker in closed:
            ledger.remove_position(ticker)
        for ticker, qty, avg_cost in rows:
            ledger.set_position(ticker, qty, avg_cost)
        if cash is not None:
            ledger.set_cash(cash)
        record_sync(conn, args=args, before=before, after=full_snapshot(ledger))


def last_sync(conn: Any) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT id, ts, source, mode FROM shadow_syncs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:  # noqa: BLE001 — a DB without the table has no sync
        return None
    return dict(row) if row is not None else None


def stale_line(conn: Any, *, stale_days: int, now: datetime | None = None,
               lang: str = "en") -> str:
    """The check-in's request for a fresh screenshot: "" while the last
    sync is recent enough."""
    now = now or datetime.now(UTC)
    last = last_sync(conn)
    if last is None:
        return tr(lang, "Your shadow ledger has never been synced with your real account. "
                        "Send a screenshot of your positions (with cash) to start.")
    try:
        then = datetime.fromisoformat(last["ts"])
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    age = now - then
    if age < timedelta(days=stale_days):
        return ""
    return tr(lang, "Your shadow ledger was last synced with your real account {days} days "
                    "ago. Send a fresh screenshot of your positions (with cash) when you can.",
              days=age.days)


# --- the chat tool ----------------------------------------------------------

def _planned(strategies: Any) -> set[str] | None:
    try:
        from allpath_trade.strategy.model import StrategyStatus

        return {d.position.ticker for d in strategies.load_all(status=StrategyStatus.ACTIVE)}
    except Exception:  # noqa: BLE001 — the "no plan" line is optional
        return None


def register_holdings_sync_tool(registry: ToolRegistry, *, ledger: Any, queue: Any,
                                strategies: Any, conversation_id_fn: Any,
                                notifier: Any = None, web_base_url: str = "",
                                app_state: Any = None, telegram_bot_token: str = "") -> None:
    """`propose_holdings_sync` for the shadow chat registry. Queues at most
    one `shadow_edit` review per call and never writes the ledger."""
    from allpath_trade.agent.shadow_tools import full_snapshot
    from allpath_trade.notify import events
    from allpath_trade.notify.dispatch import notify_review_queued

    def propose_holdings_sync(source: str = "text", positions: Any = None, text: str = "",
                              cash: Any = None, complete: bool = False,
                              unreadable: Any = None) -> str:
        lang = i18n.of(queue)
        source = source if source in SOURCES else "text"
        if isinstance(positions, str) and positions.strip():
            try:
                positions = json.loads(positions)
            except ValueError:
                return "error: positions must be a JSON list of {ticker, qty, avg_cost}"
        if text and str(text).strip():
            parsed = parse_holdings_text(str(text))
            if cash not in (None, "") and parsed.cash is None:
                _set_cash(parsed, cash, "cash")
            for note in unreadable if isinstance(unreadable, list) else []:
                parsed.problems.append(f"unreadable: {' '.join(str(note).split())[:160]}")
        else:
            parsed = parse_extraction(positions, cash, unreadable)
        if parsed.problems:
            return ("NOT QUEUED. Ask the owner about each of these, then call "
                    "propose_holdings_sync again with the corrected input. Do not guess:\n- "
                    + "\n- ".join(parsed.problems))
        mode = "full" if complete is True or str(complete).lower() == "true" else "partial"
        before = full_snapshot(ledger)
        diff = diff_holdings(before, parsed, mode=mode, planned=_planned(strategies))
        if diff.needs_cost:
            return ("NOT QUEUED. These symbols are new to the ledger and the input has no "
                    "average cost for them: " + ", ".join(diff.needs_cost) + ". Ask the "
                    "owner for the average cost of each (the stock's detail screen shows "
                    "it), then call propose_holdings_sync again. Do not use the current "
                    "price as the cost unless he says so.")
        table = render_diff(diff, source=source, lang=lang)
        title = (tr(lang, "Holdings sync: {n} change(s)", n=diff.changes) if diff.changes
                 else tr(lang, "Holdings sync: no change"))
        args = {"rows": diff.rows, "closed": [e["ticker"] for e in diff.closed],
                "cash": diff.cash_after if diff.cash_changed else None,
                "source": source, "mode": mode, "counts": diff.counts(),
                "token": secrets.token_hex(8)}
        try:
            rid = queue.add_shadow_edit(op=OP, ticker="", action=title, args=args,
                                        before=before, after=diff.after,
                                        conversation_id=conversation_id_fn())
        except Exception as exc:  # noqa: BLE001 — a queue failure is the tool's answer
            return f"error: could not queue the sync: {exc}"
        closing = tr(lang, "Nothing changes until you confirm. Strategies and stops "
                           "are not touched.")
        try:
            subject, _classic = events.review_queued(
                account="shadow", review_id=rid, ticker="", action=title, strategy_id="",
                approve_url=events.approve_link(web_base_url, rid), kind="shadow_edit",
                **i18n.lang_kw(queue))
            body = f"{table}\n\n{closing}"
            approve_url = events.approve_link(web_base_url, rid)
            if approve_url:
                body += tr(lang, "\nReview & approve: {url}", url=approve_url)
            notify_review_queued(
                queue=queue, notifier=notifier, app_state=app_state,
                telegram_bot_token=telegram_bot_token, review_id=rid, subject=subject,
                body=i18n.Localized(body, lang) if lang != "en" else body,
                account="shadow")
        except Exception as exc:  # noqa: BLE001 — the row is queued; a card is best-effort
            print(f"[holdings_sync] card for #{rid} failed: {exc}", file=sys.stderr)
        return (f"QUEUED as #{rid} for the owner's confirmation (card sent; also on the "
                "Pending page). The ledger is unchanged until he confirms. Show him this "
                f"comparison as it is:\n{table}\n{closing}")

    row = {"type": "object", "properties": {
        "ticker": {"type": "string"}, "qty": {"type": "string"},
        "avg_cost": {"type": "string",
                     "description": "Average cost per share. Leave out when the input "
                                    "does not show it; never the current price."}},
        "required": ["ticker", "qty"]}
    registry.register(
        "propose_holdings_sync",
        "Compare the owner's REAL holdings (from a screenshot you read, a CSV, or a typed "
        "list) with the shadow ledger and queue ONE reconciliation for his confirmation. "
        "Never changes the ledger itself. Returns either questions to ask him (nothing "
        "queued) or the comparison that was queued. Pass `positions` (+ `cash`, "
        "`unreadable`) for a screenshot, or `text` for pasted text / a file.",
        {"type": "object", "properties": {
            "source": {"type": "string", "enum": list(SOURCES)},
            "positions": {"type": "array", "items": row},
            "text": {"type": "string",
                     "description": "Pasted list or CSV, verbatim."},
            "cash": {"type": "string",
                     "description": "Cash balance when the input shows it."},
            "complete": {"type": "boolean",
                         "description": "True only if the input shows the WHOLE account; "
                                        "then symbols missing from it are closed. False "
                                        "updates only the listed symbols."},
            "unreadable": {"type": "array", "items": {"type": "string"},
                           "description": "Rows or numbers you could not read with "
                                          "certainty, one line each."}},
         "required": ["source", "complete"]},
        propose_holdings_sync)
