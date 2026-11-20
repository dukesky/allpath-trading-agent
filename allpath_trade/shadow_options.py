"""Option holdings in the shadow ledger (OPTION_HOLDINGS / SHADOW_OPTION_HOLDINGS).

The owner's real account holds long options; the shadow ledger used to be
shares only, so its equity was understated. With this, the ledger can HOLD
long option positions and mark them. It never trades them:

- Holdings: one row per contract (OCC symbol: underlying, call/put, strike,
  expiry), with contracts, average cost per share and a x100 multiplier.
  Entered through the same confirmed `shadow_edit` path as stock edits (chat
  tools, or `OPTION,...` rows in the Settings CSV import). Equity includes
  their value.
- Marking: the app has no option quotes. A position is valued at the last
  price the owner supplied (with its date, shown as stale when old), or at
  cost until they give one. A price is never invented. An expired contract
  is flagged for them to close out, never dropped.
- Never proposed: option positions are NOT part of `Broker.get_positions()`
  (they are read with `ShadowLedger.get_option_positions()`), so no rule,
  expiry sweep, risk check or order path ever sees them, and no tool here
  can queue an order. The agent is told not to propose option trades.
- Owner-reported trades: `shadow_record_option_trade` books the fill they
  report (after their confirmation) and returns the facts for an evaluation:
  plan for the underlying, exposure including shares (contracts x 100, not
  delta-adjusted), premium at risk as a share of equity, days to expiry,
  distance from strike.

Paper is untouched: the paper broker has no option ledger and options
trading there stays behind OPTIONS_TRADING.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from allpath_trade.broker.base import parse_occ_symbol

FLAG = "option_holdings"
MULTIPLIER = Decimal(100)
STALE_MARK_DAYS = 3
SHORT_DATED_DAYS = 21

OPTION_HOLDINGS_CHAT_NOTE = """\

Option holdings. The owner's real account holds long options, and this
ledger tracks them as holdings (shown as "option:" lines; one contract = 100
shares). Rules for you:
- Never propose, suggest queuing, or draft a rule for buying or selling an
  option. There is no tool that trades options here, and you must not ask
  for one. Options come up only when THEY bring them up.
- When they tell you they bought or sold an option, record it with
  shadow_record_option_trade (their contracts and their fill price per share),
  then give them an honest evaluation from the facts the tool returns: does
  it fit their plan for the underlying, total exposure to that name including
  shares (say "N contracts control N x 100 shares, not delta-adjusted"),
  premium at risk as a share of equity, days to expiry, how far the stock is
  from the strike, and whether it looks like an impulse or a gamble. Say it
  plainly even though the trade is already done.
- To enter or correct a holding without a trade (first import, a transfer),
  use shadow_set_option_position; to remove one that expired or was closed
  earlier, shadow_remove_option_position.
- The ledger has no option quotes. A position is valued at the last price they
  gave (shadow_mark_option) or at cost. Never make up an option price; when
  a mark is marked STALE, ask them for the current price instead of guessing.
- An option marked EXPIRED is still in the ledger on purpose: ask them what
  happened (expired worthless, exercised, sold) and record that; do not
  remove it on your own.
"""


def occ_symbol(underlying: str, right: str, strike: Any, expiry: Any) -> str:
    """The OCC symbol for a contract, e.g. ("ZZZ", "call", "150", 2027-01-15)
    -> ZZZ270115C00150000. Raises ValueError on anything that does not make
    a valid symbol."""
    root = str(underlying or "").strip().upper()
    if not (1 <= len(root) <= 6 and root.isalpha()):
        raise ValueError(f"invalid underlying {underlying!r}")
    kind = str(right or "").strip().lower()
    cp = {"call": "C", "c": "C", "put": "P", "p": "P"}.get(kind)
    if cp is None:
        raise ValueError(f"right must be call or put (got {right!r})")
    try:
        day = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry).strip())
    except ValueError:
        raise ValueError(f"expiry must be YYYY-MM-DD (got {expiry!r})") from None
    try:
        k = Decimal(str(strike).strip().replace("$", "").replace(",", ""))
    except Exception:  # noqa: BLE001
        raise ValueError(f"invalid strike {strike!r}") from None
    thousandths = k * 1000
    if not k.is_finite() or k <= 0 or thousandths != thousandths.to_integral_value() \
            or thousandths >= 10 ** 8:
        raise ValueError(f"invalid strike {strike!r}")
    symbol = f"{root}{day:%y%m%d}{cp}{int(thousandths):08d}"
    if parse_occ_symbol(symbol) is None:
        raise ValueError(f"cannot build an option symbol from {underlying} {right} "
                         f"{strike} {expiry}")
    return symbol


def _plain(value: Decimal) -> str:
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _usd(value: Decimal) -> str:
    return f"${value:,.2f}"


def describe(symbol: str) -> str:
    """`ZZZ 2027-01-15 $150 call` for an OCC symbol (the symbol itself when
    it cannot be parsed)."""
    occ = parse_occ_symbol(symbol)
    if occ is None:
        return symbol
    return f"{occ.root} {occ.expiry.isoformat()} ${_plain(occ.strike)} {occ.right}"


def view(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """One ledger option row with everything a surface shows: the contract
    in words, value, P/L, and the honesty flags (`expired`, `mark_stale`,
    `mark_source`). `row` is `ShadowLedger.get_option_rows()`'s dict."""
    now = now or datetime.now(UTC)
    occ = parse_occ_symbol(row["symbol"])
    contracts = Decimal(row["contracts"])
    avg_cost = Decimal(row["avg_cost"])
    last = Decimal(row["last_price"]) if row.get("last_price") is not None else None
    mark = last if last is not None else avg_cost
    value = contracts * MULTIPLIER * mark
    cost = contracts * MULTIPLIER * avg_cost
    mark_ts = row.get("last_price_ts")
    age_days = None
    if last is not None and mark_ts:
        try:
            then = datetime.fromisoformat(mark_ts)
            if then.tzinfo is None:
                then = then.replace(tzinfo=UTC)
            age_days = (now - then).days
        except ValueError:
            age_days = None
    dte = (occ.expiry - now.date()).days if occ is not None else None
    return {
        "symbol": row["symbol"], "label": describe(row["symbol"]),
        "underlying": occ.root if occ else row.get("underlying", ""),
        "right": occ.right if occ else "", "strike": occ.strike if occ else None,
        "expiry": occ.expiry if occ else None,
        "contracts": contracts, "shares_controlled": contracts * MULTIPLIER,
        "avg_cost": avg_cost, "mark": mark, "value": value, "cost": cost,
        "unrealized_pl": value - cost,
        "mark_source": "owner" if last is not None else "cost",
        "mark_ts": mark_ts if last is not None else None, "mark_age_days": age_days,
        "mark_stale": last is None or age_days is None or age_days >= STALE_MARK_DAYS,
        "dte": dte, "expired": dte is not None and dte < 0,
    }


def mark_note(v: dict[str, Any]) -> str:
    """How trustworthy the value is, in a few words."""
    if v["mark_source"] == "cost":
        return "valued at cost: no price supplied yet"
    when = (v["mark_ts"] or "")[:10]
    if v["mark_stale"]:
        return f"STALE mark from {when}: ask the owner for the current price"
    return f"mark from {when}"


def line(v: dict[str, Any], equity: Decimal | None = None, spot: Decimal | None = None) -> str:
    """One text line for prompts, the portfolio tool and the check-in."""
    weight = (f" ({v['value'] / equity:.1%} of equity)"
              if equity is not None and equity > 0 else "")
    head = (f"{v['label']}: {_plain(v['contracts'])} contract"
            f"{'' if v['contracts'] == 1 else 's'} "
            f"({_plain(v['shares_controlled'])} sh controlled, not delta-adjusted)")
    parts = [head,
             f"cost {_usd(v['avg_cost'])}/sh premium",
             f"mark {_usd(v['mark'])}/sh, value {_usd(v['value'])}{weight} [{mark_note(v)}]"]
    if v["expired"]:
        parts.append("EXPIRED: still in the ledger, ask the owner how it ended and "
                     "close it out")
    elif v["dte"] is not None:
        parts.append(f"{v['dte']} days to expiry")
    if spot is not None and v["strike"] is not None:
        itm = spot > v["strike"] if v["right"] == "call" else spot < v["strike"]
        parts.append(f"{v['underlying']} last {_usd(spot)}, "
                     f"{'in' if itm else 'out of'} the money by {_usd(abs(spot - v['strike']))}")
    return "; ".join(parts)


def lines_for(ledger: Any, data: Any = None, *, now: datetime | None = None,
              equity: Decimal | None = None) -> list[str]:
    """`line` for every option the ledger holds; [] for a broker without an
    option ledger (paper) or when it cannot be read."""
    getter = getattr(ledger, "get_option_rows", None)
    if getter is None:
        return []
    try:
        rows = getter()
    except Exception:  # noqa: BLE001 — optional context, never fatal
        return []
    out = []
    for row in rows:
        v = view(row, now=now)
        spot = None
        if data is not None:
            try:
                spot = Decimal(data.get_quote(v["underlying"]).price)
            except Exception:  # noqa: BLE001 — a missing quote just drops the clause
                spot = None
        out.append(line(v, equity, spot))
    return out


def evaluation_facts(ledger: Any, data: Any, strategies: Any, symbol: str, *,
                     now: datetime | None = None) -> str:
    """The facts for evaluating an option position the owner holds or just
    traded: plan for the underlying, exposure with shares, premium at risk,
    time and distance. Reads only; every number is computed here."""
    now = now or datetime.now(UTC)
    occ = parse_occ_symbol(symbol)
    if occ is None:
        return f"OPTION CHECK: {symbol} is not an option symbol"
    rows = {r["symbol"]: r for r in ledger.get_option_rows()}
    account = ledger.get_account()
    equity = Decimal(account.equity)
    shares = next((p for p in ledger.get_positions() if p.ticker == occ.root), None)
    same_name = [view(r, now=now) for r in rows.values()
                 if (parse_occ_symbol(r["symbol"]) or occ).root == occ.root
                 and parse_occ_symbol(r["symbol"]) is not None]
    out = [f"OPTION CHECK: {describe(symbol)}"]
    held = rows.get(symbol)
    if held is not None:
        v = view(held, now=now)
        out.append(f"HOLDING: {line(v, equity)}")
        out.append(f"PREMIUM AT RISK: {_usd(v['cost'])} paid for this line"
                   + (f" = {v['cost'] / equity:.1%} of equity" if equity > 0 else "")
                   + "; a long option can lose all of it")
    else:
        out.append("HOLDING: this contract is not (or no longer) in the ledger")
    controlled = sum((v["shares_controlled"] if v["right"] == "call"
                      else -v["shares_controlled"] for v in same_name), Decimal(0))
    share_qty = shares.qty if shares is not None else Decimal(0)
    premium_total = sum((v["cost"] for v in same_name), Decimal(0))
    out.append(
        f"EXPOSURE TO {occ.root}: {_plain(share_qty)} shares held"
        + (f" ({shares.market_value / equity:.1%} of equity)"
           if shares is not None and equity > 0 else "")
        + f" + options controlling {_plain(controlled)} shares net (calls minus puts, "
          "contracts x 100, NOT delta-adjusted: no delta is available)"
        + f"; total option premium on this name {_usd(premium_total)}"
        + (f" = {premium_total / equity:.1%} of equity" if equity > 0 else ""))
    dte = (occ.expiry - now.date()).days
    if dte < 0:
        out.append(f"TIME: EXPIRED {-dte} days ago")
    else:
        out.append(f"TIME: {dte} days to expiry"
                   + (f" (short-dated: under {SHORT_DATED_DAYS} days)"
                      if dte < SHORT_DATED_DAYS else ""))
    try:
        spot = Decimal(data.get_quote(occ.root).price)
        gap = (spot - occ.strike) / occ.strike * 100
        itm = spot > occ.strike if occ.right == "call" else spot < occ.strike
        out.append(f"STRIKE: {occ.root} last {_usd(spot)}, {abs(gap):.1f}% "
                   f"{'above' if gap > 0 else 'below'} the ${_plain(occ.strike)} strike "
                   f"({'in' if itm else 'out of'} the money)")
    except Exception:  # noqa: BLE001
        out.append(f"STRIKE: no quote for {occ.root}; distance from strike unknown")
    plans = []
    try:
        for d in strategies.load_all(status=None, errors=[]):
            if d.position.ticker == occ.root:
                plans.append(f"{d.id} [{d.status.value}/{d.authorization.value}]: "
                             f"{' '.join((d.thesis or '').split())[:240]}")
    except Exception:  # noqa: BLE001
        plans = []
    out.append("PLAN FOR THE UNDERLYING: " + (" | ".join(plans) if plans else
               "no strategy on file for this name (no written plan)"))
    out.append("IMPULSE CHECK (yours to make): is there a written plan this fits; is the "
               "size one they can lose entirely; is this chasing a move or making back a "
               "loss. Say what you see.")
    return "\n".join(out)
