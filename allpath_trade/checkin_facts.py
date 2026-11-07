"""The shadow check-in's live facts: an account snapshot read straight from
the broker, the data feed and the drawdown breaker at question time.

Why it exists (coordinator, 2026-11-07): the first real check-in built its
questions from the evening reflection briefing alone. That briefing lists
positions as qty + avg_entry with no current price, and its observations
can predate a breaker reset -- so the model quoted FIG's $35.87 cost basis
as its price, called $20 calls "deep in the money" with the stock at
$18.81, mis-sized CRDO and said a reset breaker had tripped. This snapshot
leads the facts and is labelled authoritative; cost basis is always named
as cost basis."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from allpath_trade.broker.base import parse_occ_symbol

_OPTION_MULTIPLIER = Decimal(100)
# Shadow's option holdings never reach the stock-only ledger; they live as
# notify strategy docs named like "FIG long call reminder ($20C exp
# 2027-01-15)" or with a thesis opening "Real long ORCL $150 call expiring
# 2027-01-15: ... 3 contracts".
_NAME_OPTION = re.compile(r"\$(\d+(?:\.\d+)?)([CP]) exp (\d{4}-\d{2}-\d{2})")
_THESIS_OPTION = re.compile(r"\$(\d+(?:\.\d+)?) (call|put) expiring (\d{4}-\d{2}-\d{2})")
_CONTRACTS = re.compile(r"(\d+) contracts?\b")
SNAPSHOT_HEADING = "## LIVE ACCOUNT SNAPSHOT"


def _usd(x: Decimal) -> str:
    return f"${x:,.2f}"


def _pct(part: Decimal, whole: Decimal, places: int = 2) -> str:
    if not whole:
        return "n/a"
    return f"{part / whole * 100:.{places}f}%"


def _signed_pct(pl: Decimal, cost: Decimal) -> str:
    if not cost:
        return "n/a"
    return f"{pl / cost * 100:+.2f}%"


def _quote(data: Any, ticker: str) -> Decimal | None:
    try:
        return Decimal(data.get_quote(ticker).price)
    except Exception:  # noqa: BLE001 — one missing quote falls back, never fails
        return None


def _stock_line(p: Any, equity: Decimal, data: Any) -> str:
    live = _quote(data, p.ticker)
    last = (_usd(live) if live is not None
            else f"{_usd(p.market_value / p.qty)} (broker mark)" if p.qty else "n/a")
    cost = p.market_value - p.unrealized_pl
    return (f"- {p.ticker}: {p.qty.normalize():f} sh, last {last}, "
            f"value {_usd(p.market_value)} ({_pct(p.market_value, equity)} of equity); "
            f"cost basis {_usd(p.avg_entry_price)}/sh "
            f"(unrealized {_signed_pct(p.unrealized_pl, cost)})")


def _option_line(p: Any, occ: Any, equity: Decimal, data: Any) -> str:
    contracts = p.qty.normalize()
    exposure = (p.qty * _OPTION_MULTIPLIER).normalize()
    mark = p.market_value / (p.qty * _OPTION_MULTIPLIER) if p.qty else Decimal(0)
    cost = p.market_value - p.unrealized_pl
    moneyness = _moneyness(data, occ.root, occ.right, occ.strike)
    return (f"- {occ.root} {occ.expiry.isoformat()} {_strike(occ.strike)} "
            f"{occ.right}: {contracts:f} contracts ({exposure:f} sh exposure), "
            f"mark {_usd(mark)}/sh, value {_usd(p.market_value)} "
            f"({_pct(p.market_value, equity)} of equity); "
            f"cost basis {_usd(p.avg_entry_price)}/sh premium "
            f"(unrealized {_signed_pct(p.unrealized_pl, cost)}); {moneyness}")


def _moneyness(data: Any, root: str, right: str, strike: Decimal) -> str:
    spot = _quote(data, root)
    if spot is None:
        return f"{root} underlying price unavailable"
    itm = spot > strike if right == "call" else spot < strike
    return (f"{root} last {_usd(spot)} -> "
            f"{'in' if itm else 'out of'} the money by {_usd(abs(spot - strike))}")


def _strike(strike: Decimal) -> str:
    return _usd(strike).removesuffix(".00")


def _doc_option_lines(strategies: Any, data: Any) -> list[str]:
    """One line per active strategy doc that describes an option held at
    the real broker. Never raises: unreadable docs just add nothing."""
    try:
        docs = strategies.load_all(status=None, errors=[])
    except Exception:  # noqa: BLE001 — optional context, never fatal
        return []
    lines = []
    for d in docs:
        if getattr(d.status, "value", d.status) != "active":
            continue
        thesis = d.thesis or ""
        m = _NAME_OPTION.search(d.name or "")
        if m:
            strike, right, expiry = m.group(1), {"C": "call", "P": "put"}[m.group(2)], m.group(3)
        elif (m := _THESIS_OPTION.search(thesis)):
            strike, right, expiry = m.groups()
        else:
            continue
        root, k = d.position.ticker, Decimal(strike)
        c = _CONTRACTS.search(thesis)
        n = int(c.group(1)) if c else 0
        size = (f"{n} contract{'' if n == 1 else 's'} per doc {d.id} "
                f"({n * 100} sh exposure)" if c
                else f"contract count not stated in doc {d.id}")
        lines.append(f"- {root} {_strike(k)} {right} exp {expiry}: {size}; "
                     f"{_moneyness(data, root, right, k)}")
    return lines


def _breaker_line(breaker: Any, equity: Decimal) -> str:
    if breaker is None:
        return "Drawdown breaker: not configured"
    halt = Decimal(breaker.halt_pct)
    if halt <= 0:
        return "Drawdown breaker: disabled"
    peak = breaker.peak()
    depth = (f"equity {_pct(peak - equity, peak, 1)} below peak {_usd(peak)}"
             if peak else "no peak recorded yet")
    tripped = breaker.tripped_at()
    if tripped:
        return (f"Drawdown breaker: TRIPPED since {tripped[:16]}Z -- {depth}; "
                f"auto strategies stay demoted to confirm until a manual reset")
    return (f"Drawdown breaker: NOT tripped -- {depth}; "
            f"trips at {halt * 100:.1f}%")


def live_snapshot(*, broker: Any, data: Any, breaker: Any, now: datetime,
                  strategies: Any = None) -> str:
    """Plain-text live snapshot. Raises when the broker cannot give the
    account or positions -- the check-in then fails and the nightly chain
    retries rather than asking questions on stale numbers. Individual quote
    misses fall back to the broker's mark."""
    acct = broker.get_account()
    positions = broker.get_positions()
    equity, cash = Decimal(acct.equity), Decimal(acct.cash)
    lines = [
        (f"{SNAPSHOT_HEADING} (as of {now.strftime('%Y-%m-%d %H:%M')} UTC; "
         "authoritative -- wins over anything below)"),
        f"equity {_usd(equity)}, cash {_usd(cash)} ({_pct(cash, equity, 1)} of equity)",
        _breaker_line(breaker, equity),
        "Positions (last = current price; cost basis = what was paid):",
    ]
    for p in positions:
        occ = parse_occ_symbol(p.ticker)
        lines.append(_option_line(p, occ, equity, data) if occ
                     else _stock_line(p, equity, data))
    if not positions:
        lines.append("- no open positions")
    doc_options = _doc_option_lines(strategies, data) if strategies is not None else []
    if doc_options:
        lines.append("Options held at the real broker (not in the ledger; size from "
                     "the strategy doc, moneyness from the live price -- any "
                     "moneyness or mark in a doc's thesis text is stale):")
        lines += doc_options
    return "\n".join(lines)
