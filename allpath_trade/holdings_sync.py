"""Holdings sync (HOLDINGS_SYNC / SHADOW_HOLDINGS_SYNC): make the shadow
ledger equal the owner's real brokerage account.

The app has no connection to the brokerage and never logs in to it. The
holdings arrive only as something the owner sends: a screenshot of the
positions screen (read by the chat model, which passes what it read as
structured rows), a CSV export, or a typed list ("NVDA 40 @ 182.5, cash
12000"). All three end in the same `ParsedHoldings`: share rows, option
lines and cash.

This extends the ledger's existing confirmed-edit path (`shadow_edit`
reviews, `agent/shadow_tools.py`) rather than adding a second one. What the
existing CSV import lacks, and this adds: screenshot and typed-list input,
closing what is no longer held, option lines, an explicit "cost unknown"
path, and a reconciliation record.

Nothing is applied silently. The chat tool `propose_holdings_sync` builds
the diff against the current ledger (new, closed, quantity or cost changed,
option lines, cash) and queues ONE `shadow_edit` review (op `sync`) whose
card shows the parsed table and the diff; the ledger changes only when the
owner confirms it. A row that could not be read, a duplicate, an ambiguous
number, or a new symbol with no cost basis is asked about instead of
guessed: nothing is queued until it is resolved. A cost the brokerage does
not show (a transferred-in position) is entered as "cost unknown": the
ledger keeps a placeholder cost and flags the position, so its P/L is never
quoted as real.

Applying writes one row to `shadow_syncs` (when, by whom, source, before,
after) and a `shadow_orders` audit row whose note starts with
"reconciliation:", the same marker a manual reconciliation uses, so a sync
can always be told apart from a trade.

Strategies are never changed or deleted here. The comparison lists holdings
with no active strategy ("no plan") and strategies whose symbol is no longer
held ("plan without a position", with the sell rules that are still on), so
the owner can decide; the agent may then draft the change the owner's rule
asks for (no position: keep entry levels, switch off trims and stops) as an
ordinary strategy revision that needs its own approval.

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
MARKER_PREFIX = "reconciliation:"

HOLDINGS_SYNC_CHAT_NOTE = """\

Holdings sync. This account's ledger is meant to equal the owner's REAL
brokerage account. When they send a screenshot of their positions, a CSV
export or a typed list of holdings and want the ledger brought in line with
it, call propose_holdings_sync ONCE with everything (not shadow_set_position
row by row):
- From a screenshot: pass `positions` with exactly what you can read
  (ticker, quantity, average cost when shown), `options` for option lines
  (underlying, call/put, strike, expiry, contracts, cost and price per share
  when shown) and `cash` when shown. Put any row or number you cannot read
  with certainty in `unreadable` (one short line each, e.g. "3rd row:
  quantity cut off"). Never fill in a guess. The market value or the
  current price is NOT the average cost; leave `avg_cost` out when the
  screen does not show it.
- From pasted text or a file: pass it verbatim as `text` (and nothing in
  `positions`).
- `complete`: true only when the input shows their WHOLE share list. If it
  may be cut off (a scrolled screen, "top holdings"), pass false: then
  nothing is closed, only the listed symbols are updated. `options_complete`
  is the same question for the option lines.
- When the owner says the brokerage shows no cost for a position (a
  transfer), set `cost_unknown` on that row. Do not invent a cost and do not
  use the current price as if it were one.
The tool never changes the ledger. It answers with questions when something
is unclear (ask the owner those and call it again with their answers), or
with the comparison it queued for their confirmation. Show them that
comparison as it is; do not re-compute it. Tell them nothing changes until
they confirm on the card. You never log in to their brokerage and never ask
for its password.
After a sync, plans and holdings may not match. The comparison lists
holdings with no plan and plans without a position. Never delete or switch
off a strategy on your own. The owner's standing rule: for a symbol they no
longer hold, keep the entry levels and switch off the trims and stops until
a position exists again. Offer exactly that as a draft_strategy revision per
strategy (set those sell rules to `state: disabled`, change nothing else),
and ask whether they want a plan drafted for each holding that has none.
Each revision needs their approval like any other.
"""


@dataclass
class ParsedHoldings:
    """What any input path produced. `rows` hold `ticker`, `qty`, `avg_cost`
    (None when the input gave no cost) and `cost_unknown`; `options` hold
    `symbol` (OCC), `contracts`, `avg_cost`, `last_price`; `problems` are
    things to ask the owner about, in plain words."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    cash: Decimal | None = None
    problems: list[str] = field(default_factory=list)


# --- parsing --------------------------------------------------------------

_NUM = r"\$?\s*-?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
_STRICT_NUM = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|-?\.\d+")
_UNKNOWN = ("?", "unknown", "n/a", "na", "未知", "不知道", "不详")
_UNKNOWN_RE = r"\?|unknown|n/a|未知|不知道|不详"
_CASH_WORDS = ("cash", "现金", "buying power", "可用资金")
_COST_MARK = r"@|at|成本|均价|avg(?:\.|\s*cost)?|cost"
_ENTRY = re.compile(
    rf"^(?P<ticker>[A-Za-z][A-Za-z0-9.\-]{{0,9}})\s*[:：]?\s+(?P<qty>{_NUM})\s*"
    r"(?:shares?|sh|股)?\s*"
    rf"(?:(?:{_COST_MARK})\s*(?P<cost>{_NUM}|{_UNKNOWN_RE}))?\s*$", re.IGNORECASE)
_UNMARKED = re.compile(
    rf"^(?P<ticker>[A-Za-z][A-Za-z0-9.\-]{{0,9}})\s+(?P<qty>{_NUM})\s*(?:shares?|sh|股)?"
    rf"\s+(?P<other>{_NUM})\s*$", re.IGNORECASE)
_CASH = re.compile(
    rf"^(?:{'|'.join(_CASH_WORDS)})\s*[:：=]?\s*(?P<amount>{_NUM})\s*$", re.IGNORECASE)
# A comma separates entries when a space or a letter follows it; "12,000"
# (a digit right after the comma) stays one number.
_SPLIT = re.compile(r"[;；\n]|[,，](?=\s)|[,，](?=[A-Za-z一-鿿])")
_HEADER_TICKER = ("ticker", "symbol", "instrument", "代码", "股票")
_HEADER_QTY = ("qty", "quantity", "shares", "数量", "股数")
# Per-share cost only. "Cost basis" in a brokerage export is usually the
# TOTAL paid for the position, so it is deliberately not accepted here; a
# file with only that column gives no cost (the ledger's cost is then kept,
# or asked for).
_HEADER_COST = ("avg_cost", "average cost", "avg cost", "average_cost", "avg. cost",
                "average price", "avg price", "cost per share", "均价", "成本价", "每股成本")


def _number(raw: Any, name: str) -> Decimal:
    """One number as a person wrote it: `$1,010.25`, `40`, `.5`. A comma is
    only a thousands separator (`182,5` is refused, not read as 1825)."""
    from allpath_trade.agent.shadow_tools import _parse_money

    text = str(raw).strip().replace("$", "").replace(" ", "")
    if not _STRICT_NUM.fullmatch(text):
        raise ValueError(f"invalid {name} {raw!r}")
    return _parse_money(text.replace(",", ""), field=name)


def _is_unknown(raw: Any) -> bool:
    return str(raw or "").strip().lower() in _UNKNOWN


def _add_row(out: ParsedHoldings, seen: set[str], ticker_raw: Any, qty_raw: Any,
             cost_raw: Any, where: str, *, cost_unknown: bool = False) -> None:
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
    if _is_unknown(cost_raw):
        cost_unknown = True
    elif cost_raw not in (None, ""):
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
                     "avg_cost": None if cost is None else str(cost),
                     "cost_unknown": bool(cost_unknown and cost is None)})


def _add_option(out: ParsedHoldings, seen: set[str], cells: list[str], where: str) -> None:
    """`cells` is an `OPTION,underlying,call|put,strike,expiry,contracts,
    avg_cost[,last_price]` row (the Settings CSV import's own format)."""
    from allpath_trade.agent.shadow_tools import _parse_option_csv_row

    row, problem = _parse_option_csv_row([c.replace("$", "").strip() for c in cells])
    if problem:
        out.problems.append(f"{where}: option line: {problem}")
        return
    if row["ticker"] in seen:
        out.problems.append(f"{where}: option {row['ticker']} appears more than once")
        return
    seen.add(row["ticker"])
    out.options.append({"symbol": row["ticker"], "contracts": row["qty"],
                        "avg_cost": row["avg_cost"], "last_price": row["last_price"]})


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
    ticker and a quantity column. A cost column is taken only when its name
    says it is per share; anything with "total" in it never is."""
    lowered = [c.strip().lower() for c in cells]
    found: dict[str, int] = {}
    for name, words in (("ticker", _HEADER_TICKER), ("qty", _HEADER_QTY),
                        ("cost", _HEADER_COST)):
        for index, cell in enumerate(lowered):
            if index in found.values() or (name == "cost" and "total" in cell):
                continue
            if any(cell == w or cell.startswith(w) for w in words):
                found[name] = index
                break
    return found if "ticker" in found and "qty" in found else None


def parse_holdings_text(text: str) -> ParsedHoldings:
    """A CSV export or a typed list. Understands a header row (symbol /
    quantity / average cost in any order), bare `TICKER,QTY,COST` rows,
    `OPTION,...` rows, `NVDA 40 @ 182.5`, `NVDA 40 shares at $182.50`,
    `NVDA 40` (no cost), `NVDA 40 @ ?` (cost unknown) and `cash 12000` /
    `CASH,12000`; entries may be separated by new lines, semicolons or
    commas. Anything else becomes a problem to ask about."""
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
        if cells and cells[0].upper() == "OPTION":
            entry_no += 1
            _add_option(out, seen, cells, f"entry {entry_no}")
            continue
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
                loose = _UNMARKED.match(piece)
                if loose is not None:
                    out.problems.append(
                        f"{where}: in {piece[:60]!r}, is {loose.group('other').strip()} the "
                        f"average cost of {loose.group('ticker').upper()}? (write it as "
                        f"'{loose.group('ticker').upper()} {loose.group('qty').strip()} @ "
                        "<cost>'; the current price is not the cost)")
                else:
                    out.problems.append(f"{where}: could not read {piece[:60]!r}")
                continue
            _add_row(out, seen, entry.group("ticker"), entry.group("qty"),
                     entry.group("cost"), where)
        if len(out.rows) > MAX_ROWS:
            out.problems.append(f"more than {MAX_ROWS} positions")
            break
    if not out.rows and not out.options and out.cash is None and not out.problems:
        out.problems.append("no holdings found in the text")
    return out


def parse_extraction(positions: Any, cash: Any = None, unreadable: Any = None,
                     options: Any = None) -> ParsedHoldings:
    """What the vision model read off a screenshot: `positions` is a list of
    `{ticker, qty, avg_cost?, cost_unknown?}`, `options` a list of
    `{underlying, right, strike, expiry, contracts, avg_cost, last_price?}`,
    `unreadable` its own list of rows it could not read. Every value is
    validated here; nothing is repaired."""
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
                 row.get("avg_cost", row.get("average_cost")), where,
                 cost_unknown=row.get("cost_unknown") is True
                 or str(row.get("cost_unknown")).lower() == "true")
    if len(positions) > MAX_ROWS:
        out.problems.append(f"more than {MAX_ROWS} positions")
    if not isinstance(options, list):
        if options not in (None, ""):
            out.problems.append("options must be a list of option lines")
        options = []
    for index, row in enumerate(options[:MAX_ROWS], 1):
        where = f"option {index}"
        if not isinstance(row, dict):
            out.problems.append(f"{where}: not an option line")
            continue
        cells = ["OPTION", *(str(row.get(k, "") if row.get(k) is not None else "")
                             for k in ("underlying", "right", "strike", "expiry",
                                       "contracts", "avg_cost"))]
        if row.get("last_price") not in (None, ""):
            cells.append(str(row["last_price"]))
        if cells[6] == "":
            out.problems.append(
                f"{where}: no cost per share for the {cells[1]} {cells[3]} {cells[2]} "
                "option (ask the owner; never use the current price as the cost)")
            continue
        _add_option(out, seen, cells, where)
    if cash not in (None, ""):
        _set_cash(out, cash, "cash")
    if not out.rows and not out.options and out.cash is None and not out.problems:
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
    cost_unknown: list[str] = field(default_factory=list)  # flagged "cost unknown" after
    cash_before: str = "0"
    cash_after: str | None = None                         # None = cash not in the input
    needs_cost: list[str] = field(default_factory=list)   # new symbol without a cost
    after: dict[str, Any] = field(default_factory=dict)   # full_snapshot shape
    rows: list[dict[str, Any]] = field(default_factory=list)  # what applying will set
    # option lines
    options_new: list[dict[str, Any]] = field(default_factory=list)
    options_closed: list[dict[str, Any]] = field(default_factory=list)
    options_changed: list[dict[str, Any]] = field(default_factory=list)
    options_marked: list[dict[str, Any]] = field(default_factory=list)
    options_unchanged: list[str] = field(default_factory=list)
    options_not_listed: list[str] = field(default_factory=list)
    option_rows: list[dict[str, Any]] = field(default_factory=list)
    # plans (filled by `plan_report`)
    no_plan: list[str] = field(default_factory=list)
    plans_without_position: list[dict[str, Any]] = field(default_factory=list)
    sell_rules_off: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cash_changed(self) -> bool:
        return self.cash_after is not None and Decimal(self.cash_after) != Decimal(
            self.cash_before)

    @property
    def option_changes(self) -> int:
        return (len(self.options_new) + len(self.options_closed)
                + len(self.options_changed) + len(self.options_marked))

    @property
    def changes(self) -> int:
        return (len(self.new) + len(self.closed) + len(self.qty_changed)
                + len(self.cost_changed) + int(self.cash_changed) + self.option_changes)

    def counts(self) -> dict[str, int]:
        return {"new": len(self.new), "closed": len(self.closed),
                "qty_changed": len(self.qty_changed),
                "cost_changed": len(self.cost_changed),
                "cash_changed": int(self.cash_changed),
                "unchanged": len(self.unchanged),
                "cost_unknown": len(self.cost_unknown),
                "option_lines": self.option_changes}


def diff_holdings(before: dict[str, Any], parsed: ParsedHoldings, *, mode: str = "full",
                  planned: set[str] | None = None,
                  options_before: dict[str, dict[str, Any]] | None = None,
                  options_complete: bool = False,
                  cost_unknown_before: set[str] | None = None,
                  placeholder_cost: Any = None) -> SyncDiff:
    """`before` is `shadow_tools.full_snapshot`'s dict; `options_before` maps
    an OCC symbol to its `option_snapshot`. `planned` is the set of tickers
    with an active strategy (None leaves `no_plan` empty). `placeholder_cost`
    is a `ticker -> Decimal | None` callable giving the price used as the
    placeholder cost of a NEW position entered as "cost unknown"."""
    diff = SyncDiff(mode=mode, cash_before=str(before.get("cash", "0")))
    old = {t: dict(p) for t, p in (before.get("positions") or {}).items()}
    unknown_before = set(cost_unknown_before or ())
    after_positions = {} if mode == "full" else {t: dict(p) for t, p in old.items()}
    unknown_after = set() if mode == "full" else set(unknown_before)
    listed = set()
    for row in parsed.rows:
        ticker, qty = row["ticker"], Decimal(row["qty"])
        listed.add(ticker)
        held = old.get(ticker)
        cost = row.get("avg_cost")
        unknown = bool(row.get("cost_unknown"))
        if qty == 0:
            if held is not None:
                diff.closed.append({"ticker": ticker, **held})
                after_positions.pop(ticker, None)
                unknown_after.discard(ticker)
            continue
        if held is None:
            if cost is None and unknown and placeholder_cost is not None:
                price = placeholder_cost(ticker)
                cost = None if price is None else str(price)
            if cost is None:
                diff.needs_cost.append(ticker)
                continue
            diff.new.append({"ticker": ticker, "qty": row["qty"], "avg_cost": cost})
        else:
            if cost is None:
                cost = held["avg_cost"]
                if not unknown:
                    diff.cost_kept.append(ticker)
                unknown = unknown or ticker in unknown_before
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
                if unknown:
                    unknown_after.add(ticker)
                else:
                    unknown_after.discard(ticker)
                if unknown != (ticker in unknown_before):
                    # Only the "cost unknown" mark changes: still a row to apply.
                    diff.rows.append({"ticker": ticker, "qty": held["qty"],
                                      "avg_cost": held["avg_cost"], "cost_unknown": unknown})
                continue
        if unknown:
            unknown_after.add(ticker)
        else:
            unknown_after.discard(ticker)
        after_positions[ticker] = {"qty": row["qty"], "avg_cost": cost}
        diff.rows.append({"ticker": ticker, "qty": row["qty"], "avg_cost": cost,
                          "cost_unknown": unknown})
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
    diff.cost_unknown = sorted(t for t in unknown_after if t in after_positions)

    # option lines
    held_options = {s: dict(o) for s, o in (options_before or {}).items()}
    listed_options = set()
    for line in parsed.options:
        symbol = line["symbol"]
        listed_options.add(symbol)
        held = held_options.get(symbol)
        contracts = Decimal(line["contracts"])
        entry = {"symbol": symbol, "contracts": line["contracts"],
                 "avg_cost": line["avg_cost"], "last_price": line.get("last_price")}
        if contracts == 0:
            if held is not None:
                diff.options_closed.append({"symbol": symbol, **held})
            continue
        if held is None:
            diff.options_new.append(entry)
        else:
            moved = (Decimal(held["contracts"]) != contracts
                     or Decimal(held["avg_cost"]) != Decimal(line["avg_cost"]))
            price_moved = (line.get("last_price") is not None
                           and (held.get("last_price") is None
                                or Decimal(held["last_price"]) != Decimal(line["last_price"])))
            if moved:
                diff.options_changed.append({**entry, "old_contracts": held["contracts"],
                                             "old_avg_cost": held["avg_cost"]})
            elif price_moved:
                diff.options_marked.append({**entry, "old_last_price": held.get("last_price")})
            else:
                diff.options_unchanged.append(symbol)
                continue
        diff.option_rows.append(entry)
    for symbol, held in held_options.items():
        if symbol in listed_options:
            continue
        if options_complete:
            diff.options_closed.append({"symbol": symbol, **held})
        else:
            diff.options_not_listed.append(symbol)

    if planned is not None:
        diff.no_plan = sorted(t for t in after_positions if t not in planned)
    for key in ("new", "closed", "qty_changed", "cost_changed"):
        getattr(diff, key).sort(key=lambda e: e["ticker"])
    for key in ("options_new", "options_closed", "options_changed", "options_marked"):
        getattr(diff, key).sort(key=lambda e: e["symbol"])
    return diff


def plan_report(strategies: Any, held: set[str], option_underlyings: set[str]) -> dict[str, Any]:
    """Where plans and holdings do not match, for the owner to decide:
    `no_plan` (held, no active strategy), `plans_without_position` (an
    active strategy whose symbol is not held as shares or options, with the
    stock sell rules still switched on) and `sell_rules_off` (a held symbol
    whose strategy has sell rules switched off). Reads only; never raises."""
    out: dict[str, Any] = {"no_plan": [], "plans_without_position": [], "sell_rules_off": []}
    try:
        from allpath_trade.strategy.actions import ActionError, ActionKind, parse_action
        from allpath_trade.strategy.model import RuleState, StrategyStatus

        docs = strategies.load_all(status=StrategyStatus.ACTIVE, errors=[])
    except Exception:  # noqa: BLE001 — the report is optional
        return out
    sells = (ActionKind.SELL_ALL, ActionKind.SELL_PCT, ActionKind.SELL_VALUE)
    planned = set()
    for doc in sorted(docs, key=lambda d: str(getattr(d, "id", ""))):
        if not hasattr(doc, "rules"):      # not a strategy document: only its ticker counts
            planned.add(doc.position.ticker)
            continue
        ticker = doc.position.ticker
        planned.add(ticker)
        sell_rules = []
        for rule in doc.rules:
            try:
                if parse_action(rule.action).kind in sells:
                    sell_rules.append(rule)
            except ActionError:
                continue
        on = [r.id for r in sell_rules if r.state != RuleState.DISABLED]
        off = [r.id for r in sell_rules if r.state == RuleState.DISABLED]
        if ticker not in held and ticker not in option_underlyings:
            out["plans_without_position"].append(
                {"strategy_id": doc.id, "ticker": ticker, "sell_rules_on": on})
        elif ticker in held and off:
            out["sell_rules_off"].append(
                {"strategy_id": doc.id, "ticker": ticker, "sell_rules_off": off})
    out["no_plan"] = sorted(t for t in held if t not in planned)
    return out


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
    "Cost unknown (placeholder cost kept; P/L for these is not real): {tickers}":
        "成本未知（用占位成本，这些的盈亏不是真实的）：{tickers}",
    "Options:": "期权：",
    "  new  {label}  {contracts} x @ {cost}": "  新增  {label}  {contracts} 张 @ {cost}",
    "  closed  {label}  was {contracts} x @ {cost}":
        "  移除  {label}  原 {contracts} 张 @ {cost}",
    "  changed  {label}  {old_contracts} → {contracts} x, cost {old_cost} → {cost}":
        "  变化  {label}  {old_contracts} → {contracts} 张，成本 {old_cost} → {cost}",
    "  price  {label}  {old} → {new}": "  现价  {label}  {old} → {new}",
    "  unchanged: {n}": "  没变：{n} 行",
    "  not in the input, left as they are: {n}": "  输入里没有，保持不变：{n} 行",
    "No plan (held, no active strategy): {tickers}": "没有计划（有持仓但没有生效的策略）：{tickers}",
    "Plans without a position:": "有计划但没有持仓：",
    "  {strategy_id} ({ticker}): sell rules still on: {rules}":
        "  {strategy_id}（{ticker}）：卖出规则仍开着：{rules}",
    "  {strategy_id} ({ticker}): no sell rule switched on":
        "  {strategy_id}（{ticker}）：没有开着的卖出规则",
    "Held, but stops/trims switched off:": "有持仓，但止损/减仓规则是关着的：",
    "  {strategy_id} ({ticker}): {rules}": "  {strategy_id}（{ticker}）：{rules}",
    "The ledger already matches. Confirming records the reconciliation.":
        "账本已经和输入一致。确认后会记录这次对账。",
    "Nothing changes until you confirm. Strategies and stops are not touched.":
        "你确认之前账本不会变。策略和止损不受影响。",
    "After you confirm, tell me what to do about the plans listed above. I will not "
    "change or delete a plan on my own.":
        "确认之后告诉我上面列出的计划怎么处理。我不会自己修改或删除任何计划。",
    "Holdings sync: {n} change(s)": "持仓同步：{n} 处变化",
    "Holdings sync: no change": "持仓同步：无变化",
    "Your shadow ledger was last synced with your real account {days} days ago. "
    "Send a fresh screenshot of your positions (with cash) when you can.":
        "shadow 账本上次和真实账户对账是 {days} 天前。方便时请发一张最新的持仓截图（带现金）。",
    "Your shadow ledger has never been synced with your real account. "
    "Send a screenshot of your positions (with cash) to start.":
        "shadow 账本还没有和真实账户对过账。请发一张持仓截图（带现金）开始。",
})


def _money(value: Any) -> str:
    from allpath_trade.compact_cards import money

    return money(value) if value is not None else "-"


def _qty(value: Any) -> str:
    number = Decimal(str(value))
    text = f"{number:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _label(symbol: str) -> str:
    from allpath_trade.shadow_options import describe

    return describe(symbol)


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
            if Decimal(e["old_avg_cost"]) == Decimal(e["avg_cost"]):
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
    if diff.cost_unknown:
        lines.append(t("Cost unknown (placeholder cost kept; P/L for these is not real): "
                       "{tickers}", tickers=", ".join(diff.cost_unknown)))
    if (diff.option_changes or diff.options_unchanged or diff.options_not_listed):
        lines.append(t("Options:"))
        lines += [t("  new  {label}  {contracts} x @ {cost}", label=_label(e["symbol"]),
                    contracts=_qty(e["contracts"]), cost=_money(e["avg_cost"]))
                  for e in diff.options_new]
        lines += [t("  closed  {label}  was {contracts} x @ {cost}", label=_label(e["symbol"]),
                    contracts=_qty(e["contracts"]), cost=_money(e["avg_cost"]))
                  for e in diff.options_closed]
        lines += [t("  changed  {label}  {old_contracts} → {contracts} x, cost {old_cost} → "
                    "{cost}", label=_label(e["symbol"]),
                    old_contracts=_qty(e["old_contracts"]), contracts=_qty(e["contracts"]),
                    old_cost=_money(e["old_avg_cost"]), cost=_money(e["avg_cost"]))
                  for e in diff.options_changed]
        lines += [t("  price  {label}  {old} → {new}", label=_label(e["symbol"]),
                    old=_money(e.get("old_last_price")), new=_money(e["last_price"]))
                  for e in diff.options_marked]
        if diff.options_unchanged:
            lines.append(t("  unchanged: {n}", n=len(diff.options_unchanged)))
        if diff.options_not_listed:
            lines.append(t("  not in the input, left as they are: {n}",
                           n=len(diff.options_not_listed)))
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
    if diff.plans_without_position:
        lines.append(t("Plans without a position:"))
        for plan in diff.plans_without_position:
            if plan["sell_rules_on"]:
                lines.append(t("  {strategy_id} ({ticker}): sell rules still on: {rules}",
                               strategy_id=plan["strategy_id"], ticker=plan["ticker"],
                               rules=", ".join(plan["sell_rules_on"])))
            else:
                lines.append(t("  {strategy_id} ({ticker}): no sell rule switched on",
                               strategy_id=plan["strategy_id"], ticker=plan["ticker"]))
    if diff.sell_rules_off:
        lines.append(t("Held, but stops/trims switched off:"))
        lines += [t("  {strategy_id} ({ticker}): {rules}", strategy_id=plan["strategy_id"],
                    ticker=plan["ticker"], rules=", ".join(plan["sell_rules_off"]))
                  for plan in diff.sell_rules_off]
    if diff.changes == 0:
        lines.append(t("The ledger already matches. Confirming records the reconciliation."))
    return "\n".join(lines)


def closing_lines(diff: SyncDiff, lang: str = "en") -> str:
    text = tr(lang, "Nothing changes until you confirm. Strategies and stops are not touched.")
    if diff.no_plan or diff.plans_without_position or diff.sell_rules_off:
        text += "\n" + tr(lang, "After you confirm, tell me what to do about the plans listed "
                                "above. I will not change or delete a plan on my own.")
    return text


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
        " before, after, n_new, n_closed, n_qty_changed, n_cost_changed, cash_changed,"
        " n_cost_unknown, n_option_lines)"
        " VALUES (?, 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now.isoformat(), str(args.get("source") or ""), str(args.get("mode") or "full"),
         "owner", review_id, token, json.dumps(before, default=str),
         json.dumps(after, default=str), int(counts.get("new", 0)),
         int(counts.get("closed", 0)), int(counts.get("qty_changed", 0)),
         int(counts.get("cost_changed", 0)), int(counts.get("cash_changed", 0)),
         int(counts.get("cost_unknown", 0)), int(counts.get("option_lines", 0))))
    return int(cur.lastrowid)


def option_state(ledger: Any) -> dict[str, dict[str, Any]]:
    """Every option line the ledger holds, by OCC symbol (the shape
    `diff_holdings` takes as `options_before` and the applier re-checks)."""
    getter = getattr(ledger, "get_option_rows", None)
    if getter is None:
        return {}
    return {r["symbol"]: {"contracts": r["contracts"], "avg_cost": r["avg_cost"],
                          "last_price": r["last_price"]} for r in getter()}


def apply_sync(ledger: Any, conn: Any, args: dict[str, Any], before: Any) -> None:
    """The approved sync, in one transaction: close what is gone, set what
    is new or changed (shares, cost-unknown flags, option lines), set cash,
    write the reconciliation record and its marker row. The caller
    (`apply_shadow_edit_factory`) has already checked the share book for
    staleness; the option book is checked here."""
    from allpath_trade.agent.shadow_tools import _normalize_snapshot, _parse_money, full_snapshot
    from allpath_trade.store.reviews import RevisionValidationError

    options_before = args.get("options_before")
    if options_before is not None and _normalize_snapshot(
            option_state(ledger)) != _normalize_snapshot(options_before):
        raise RevisionValidationError("ledger changed since this proposal — re-propose")
    rows = [(row["ticker"], _parse_money(row["qty"], field="qty"),
             _parse_money(row["avg_cost"], field="avg_cost"), bool(row.get("cost_unknown")))
            for row in args["rows"]]
    closed = [str(t) for t in args.get("closed", [])]
    option_rows = [
        (line["symbol"], _parse_money(line["contracts"], field="contracts"),
         _parse_money(line["avg_cost"], field="avg_cost"),
         (_parse_money(line["last_price"], field="last_price")
          if line.get("last_price") is not None else None))
        for line in args.get("option_rows", [])]
    options_closed = [str(s) for s in args.get("options_closed", [])]
    cash = (_parse_money(args["cash"], field="cash")
            if args.get("cash") is not None else None)
    counts = args.get("counts") or {}
    with conn.transaction():
        # The marker first, so the per-position audit rows follow it (the
        # same layout a manual reconciliation leaves in shadow_orders).
        ledger.note_reconciliation(
            f"{MARKER_PREFIX} holdings sync from {args.get('source') or 'input'} "
            f"({args.get('mode') or 'full'}), confirmed by the owner; "
            f"{counts.get('new', 0)} new, {counts.get('closed', 0)} closed, "
            f"{counts.get('qty_changed', 0)} quantity changed, "
            f"{counts.get('cost_changed', 0)} cost changed, "
            f"{counts.get('option_lines', 0)} option line(s), cash "
            f"{'changed' if counts.get('cash_changed') else 'unchanged'}")
        for ticker in closed:
            ledger.remove_position(ticker)
        for ticker, qty, avg_cost, unknown in rows:
            ledger.set_position(ticker, qty, avg_cost)
            ledger.set_cost_unknown(ticker, unknown)
        for symbol in options_closed:
            ledger.remove_option_position(symbol)
        for symbol, contracts, avg_cost, last in option_rows:
            ledger.set_option_position(symbol, contracts, avg_cost, last)
        if cash is not None:
            ledger.set_cash(cash)
        after = full_snapshot(ledger)
        record_sync(conn, args=args, before={**before, "options": options_before or {}},
                    after={**after, "options": option_state(ledger)})


def last_sync(conn: Any) -> dict[str, Any] | None:
    """The most recent reconciliation: an applied sync, or a manual one (a
    `shadow_orders` row whose note starts with "reconciliation:")."""
    found: list[dict[str, Any]] = []
    try:
        row = conn.execute(
            "SELECT ts, source FROM shadow_syncs ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            found.append(dict(row))
    except Exception:  # noqa: BLE001, S110 — a DB without the table has no sync
        pass
    try:
        row = conn.execute(
            "SELECT ts, 'manual' AS source FROM shadow_orders WHERE side = 'adjust'"
            " AND note LIKE ? ORDER BY id DESC LIMIT 1", (MARKER_PREFIX + "%",)).fetchone()
        if row is not None:
            found.append(dict(row))
    except Exception:  # noqa: BLE001, S110
        pass
    return max(found, key=lambda r: r["ts"]) if found else None


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
                                strategies: Any, conversation_id_fn: Any, data: Any = None,
                                notifier: Any = None, web_base_url: str = "",
                                app_state: Any = None, telegram_bot_token: str = "") -> None:
    """`propose_holdings_sync` for the shadow chat registry. Queues at most
    one `shadow_edit` review per call and never writes the ledger."""
    from allpath_trade.agent.shadow_tools import full_snapshot
    from allpath_trade.broker.base import parse_occ_symbol
    from allpath_trade.notify import events
    from allpath_trade.notify.dispatch import notify_review_queued

    def _placeholder(ticker: str) -> Decimal | None:
        try:
            return Decimal(data.get_quote(ticker).price) if data is not None else None
        except Exception:  # noqa: BLE001 — no quote: the cost is then asked for
            return None

    def _true(value: Any) -> bool:
        return value is True or str(value).lower() == "true"

    def propose_holdings_sync(source: str = "text", positions: Any = None, text: str = "",
                              cash: Any = None, complete: bool = False,
                              unreadable: Any = None, options: Any = None,
                              options_complete: bool = False) -> str:
        lang = i18n.of(queue)
        source = source if source in SOURCES else "text"
        for name, value in (("positions", positions), ("options", options)):
            if isinstance(value, str) and value.strip():
                try:
                    value = json.loads(value)
                except ValueError:
                    return f"error: {name} must be a JSON list"
                if name == "positions":
                    positions = value
                else:
                    options = value
        has_text = bool(text and str(text).strip())
        if has_text and (positions or options):
            return ("error: pass EITHER `text` (pasted list or file) OR `positions` / "
                    "`options` (what you read from a screenshot), not both; nothing was "
                    "queued")
        if has_text:
            parsed = parse_holdings_text(str(text))
            if cash not in (None, "") and parsed.cash is None:
                _set_cash(parsed, cash, "cash")
            for note in unreadable if isinstance(unreadable, list) else []:
                parsed.problems.append(f"unreadable: {' '.join(str(note).split())[:160]}")
        else:
            parsed = parse_extraction(positions, cash, unreadable, options)
        if parsed.problems:
            return ("NOT QUEUED. Ask the owner about each of these, then call "
                    "propose_holdings_sync again with the corrected input. Do not guess:\n- "
                    + "\n- ".join(parsed.problems))
        mode = "full" if _true(complete) else "partial"
        before = full_snapshot(ledger)
        options_before = option_state(ledger)
        unknown_before = set(getattr(ledger, "cost_unknown_tickers", list)())
        diff = diff_holdings(before, parsed, mode=mode, planned=_planned(strategies),
                             options_before=options_before,
                             options_complete=_true(options_complete),
                             cost_unknown_before=unknown_before,
                             placeholder_cost=_placeholder)
        if diff.needs_cost:
            return ("NOT QUEUED. These symbols are new to the ledger and the input has no "
                    "average cost for them: " + ", ".join(diff.needs_cost) + ". Ask the "
                    "owner for the average cost of each (the stock's detail screen shows "
                    "it). If the brokerage shows none (a transferred-in position), call "
                    "again with cost_unknown on that row (in text: 'TICKER QTY @ ?'): the "
                    "ledger then keeps a placeholder cost and marks the position. Never "
                    "pass the current price as if it were the cost.")
        held_after = set(diff.after["positions"])
        option_roots = {occ.root for s in (set(options_before) | {e["symbol"]
                                                                  for e in diff.option_rows})
                        - {e["symbol"] for e in diff.options_closed}
                        if (occ := parse_occ_symbol(s)) is not None}
        report = plan_report(strategies, held_after, option_roots)
        diff.no_plan = report["no_plan"]
        diff.plans_without_position = report["plans_without_position"]
        diff.sell_rules_off = report["sell_rules_off"]
        table = render_diff(diff, source=source, lang=lang)
        title = (tr(lang, "Holdings sync: {n} change(s)", n=diff.changes) if diff.changes
                 else tr(lang, "Holdings sync: no change"))
        args = {"rows": diff.rows, "closed": [e["ticker"] for e in diff.closed],
                "option_rows": diff.option_rows,
                "options_closed": [e["symbol"] for e in diff.options_closed],
                "options_before": options_before,
                "cash": diff.cash_after if diff.cash_changed else None,
                "source": source, "mode": mode, "counts": diff.counts(),
                "token": secrets.token_hex(8)}
        try:
            rid = queue.add_shadow_edit(op=OP, ticker="", action=title, args=args,
                                        before=before, after=diff.after,
                                        conversation_id=conversation_id_fn())
        except Exception as exc:  # noqa: BLE001 — a queue failure is the tool's answer
            return f"error: could not queue the sync: {exc}"
        closing = closing_lines(diff, lang)
        try:
            approve_url = events.approve_link(web_base_url, rid)
            subject, _classic = events.review_queued(
                account="shadow", review_id=rid, ticker="", action=title, strategy_id="",
                approve_url=approve_url, kind="shadow_edit", **i18n.lang_kw(queue))
            body = f"{table}\n\n{closing}"
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
                "Pending page). The ledger is unchanged until they confirm. Show them this "
                f"comparison as it is:\n{table}\n{closing}")

    row = {"type": "object", "properties": {
        "ticker": {"type": "string"}, "qty": {"type": "string"},
        "avg_cost": {"type": "string",
                     "description": "Average cost per share. Leave out when the input "
                                    "does not show it; never the current price."},
        "cost_unknown": {"type": "boolean",
                         "description": "True only when the owner says the brokerage "
                                        "shows no cost for this position."}},
        "required": ["ticker", "qty"]}
    option = {"type": "object", "properties": {
        "underlying": {"type": "string"}, "right": {"type": "string", "enum": ["call", "put"]},
        "strike": {"type": "string"}, "expiry": {"type": "string", "description": "YYYY-MM-DD"},
        "contracts": {"type": "string"},
        "avg_cost": {"type": "string", "description": "Premium paid per share."},
        "last_price": {"type": "string",
                       "description": "Current price per share, only if the input shows it."}},
        "required": ["underlying", "right", "strike", "expiry", "contracts"]}
    registry.register(
        "propose_holdings_sync",
        "Compare the owner's REAL holdings (from a screenshot you read, a CSV, or a typed "
        "list) with the shadow ledger and queue ONE reconciliation for their confirmation. "
        "Never changes the ledger itself. Returns either questions to ask them (nothing "
        "queued) or the comparison that was queued. Pass `positions` / `options` (+ `cash`, "
        "`unreadable`) for a screenshot, or `text` for pasted text / a file; never both.",
        {"type": "object", "properties": {
            "source": {"type": "string", "enum": list(SOURCES)},
            "positions": {"type": "array", "items": row},
            "options": {"type": "array", "items": option},
            "text": {"type": "string",
                     "description": "Pasted list or CSV, verbatim."},
            "cash": {"type": "string",
                     "description": "Cash balance when the input shows it."},
            "complete": {"type": "boolean",
                         "description": "True only if the input shows the WHOLE share list; "
                                        "then symbols missing from it are closed. False "
                                        "updates only the listed symbols."},
            "options_complete": {"type": "boolean",
                                 "description": "True only if the input shows ALL option "
                                                "lines; then missing ones are removed."},
            "unreadable": {"type": "array", "items": {"type": "string"},
                           "description": "Rows or numbers you could not read with "
                                          "certainty, one line each."}},
         "required": ["source", "complete"]},
        propose_holdings_sync)
