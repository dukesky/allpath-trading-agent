"""Advisor for the owner's real account (shadow only).

PRE_TRADE_CONSULT / SHADOW_PRE_TRADE_CONSULT: when the owner says he wants
to buy or sell something, the chat agent first calls `pre_trade_check`. The
facts are computed here in code, with no model call: whether a plan exists
for the name (strategy thesis, his recorded views), position size and cash
after the trade, distance to the name's own stop / entry / take-profit
levels, the recent price move, and the impulse flags (no plan, chasing a
large same-day move, buying below his own stop, re-buying right after a
stop-out, oversize, not enough cash, selling more than he holds). A verdict
floor (go / wait / dont) follows from the flags; the agent may be stricter,
never looser. When he reports that he made the trade, `record_real_trade`
queues a ledger entry at HIS price and quantity for one-tap confirmation
(`ShadowLedger.record_real_fill`): the ledger never books a simulated fill
for a real trade. Every consult is logged in `trade_consults`.

ACT_PROMPT_CARDS / SHADOW_ACT_PROMPT_CARDS: a rule that fires (stop, entry
zone, take-profit) is worded as a prompt to act at the broker, and its
Telegram card has three buttons, built on the existing review card and
`rv:` callbacks: Done @ <trigger price> (books the fill at that price),
Skipped (ledger unchanged), Discuss (the agent explains, in the card's
thread). Replying to the card with a number books that price instead. With
the flag on, an `auto` strategy on that account is dispatched as `confirm`
(in memory only), so the ledger never changes before the owner has acted.

The app never places a real order and never connects to a brokerage. With
both flags off nothing here runs.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from allpath_trade import i18n
from allpath_trade.i18n import tr

if TYPE_CHECKING:
    from allpath_trade.agent.tools import ToolRegistry

FLAG = "pre_trade_consult"
ACT_FLAG = "act_prompt_cards"
OP = "real_fill"
VISION_SUBJECT = "VISION"

CHASE_DAY_PCT = Decimal(5)       # a same-day move this large is "chasing"
REBUY_COOLDOWN_DAYS = 5          # re-buying this soon after a stop-out is flagged
BLOCKING = ("oversell", "cash_short", "below_own_stop", "rebuy_after_stop")
CAUTION = ("no_plan", "chasing", "oversize")

PRE_TRADE_CHAT_NOTE = """\

Pre-trade consult. This ledger mirrors the owner's REAL account; he places
every order himself at his broker, and you never do. When he says he wants
to, is about to, or is thinking of buying or selling something (in any
language: "I want to buy X", "thinking of selling Y", 我想买, 要不要卖,
加仓, 清仓), FIRST call pre_trade_check, before any other tool and before you
give an opinion. Do not call propose_order or shadow_set_position for it.
Then look for known upcoming events with web_search (an earnings date in the
next two weeks, a known catalyst); if you could not check, say so.
Answer in this shape, short enough for a phone:
1. Verdict, first line: go / wait / don't. Never looser than the tool's
   floor; you may be stricter and must say why.
2. The ONE condition that would change the verdict.
3. Why: does it fit his plan (quote his thesis, view or VISION entry, or say
   plainly that no plan exists), size and cash after the trade, how far the
   price is from his own stop / entry / take-profit levels, the recent
   move, events.
4. Impulse check: name each flag the tool raised. You have no position and
   no feelings about this trade. If it looks like chasing, revenge for a
   loss, boredom or a gamble, say that in one direct sentence. No lecture.
5. Offer three next steps: (a) he does it and tells you the real fill; then
   call record_real_trade with HIS price and quantity; (b) set an alert at a
   level (a draft_strategy alert rule); (c) drop it.
Never record a trade he has not told you he actually made, and never at a
price you looked up. If he reports a trade already done without having
asked first, call record_real_trade, then run pre_trade_check anyway and
tell him honestly how it fits, labelled as after the fact. A long-term view
he states for the whole book goes into record_view with subject VISION
(expires_days 180, the longest a view lasts) when that tool is available.
"""


# --- the consult ----------------------------------------------------------

@dataclass
class Consult:
    ticker: str
    side: str
    qty: Decimal | None
    price: Decimal | None
    value: Decimal | None
    price_source: str = ""
    plan: dict[str, Any] | None = None
    views: list[str] = field(default_factory=list)
    held_qty: Decimal = Decimal(0)
    avg_cost: Decimal | None = None
    equity: Decimal | None = None
    weight_before: Decimal | None = None
    weight_after: Decimal | None = None
    weight_limit: Decimal | None = None
    cash_before: Decimal | None = None
    cash_after: Decimal | None = None
    min_cash_reserve: Decimal = Decimal(0)
    levels: list[dict[str, Any]] = field(default_factory=list)
    moves: dict[str, Decimal] = field(default_factory=dict)
    flags: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    consult_id: int | None = None

    @property
    def flag_codes(self) -> list[str]:
        return [f["code"] for f in self.flags]

    @property
    def floor(self) -> str:
        codes = self.flag_codes
        if any(code in BLOCKING for code in codes):
            return "dont"
        if any(code in CAUTION for code in codes):
            return "wait"
        return "go"


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).strip().replace(",", "").replace("$", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _pct(a: Decimal, b: Decimal) -> Decimal:
    return ((a - b) / b * 100).quantize(Decimal("0.1")) if b else Decimal(0)


def _level_kind(rule: Any, op: str) -> str:
    from allpath_trade.strategy.actions import ActionError, ActionKind, parse_action

    try:
        kind = parse_action(rule.action).kind
    except ActionError:
        return "alert"
    falling = op in ("<", "≤")
    if kind in (ActionKind.SELL_ALL, ActionKind.SELL_PCT, ActionKind.SELL_VALUE):
        return "stop" if falling else "take_profit"
    if kind in (ActionKind.BUY_VALUE, ActionKind.BUY_TO_TARGET):
        return "entry" if falling else "breakout_entry"
    return "alert"


def price_levels(doc: Any, price: Decimal | None) -> list[dict[str, Any]]:
    """The price thresholds in a strategy's rules, each with what it is (a
    stop, an entry, a take-profit) and how far the current price is from it.
    Only conditions made of plain `name <op> number` terms are read."""
    from allpath_trade.compact_cards import _comparisons

    out = []
    for rule in doc.rules:
        for name, op, number in _comparisons(rule.condition) or []:
            if name != "price":
                continue
            entry = {"strategy_id": doc.id, "rule_id": rule.id, "level": number, "op": op,
                     "kind": _level_kind(rule, op), "state": rule.state.value,
                     "action": rule.action}
            if price:
                entry["distance_pct"] = _pct(price, number)
            out.append(entry)
    return out


def _moves(data: Any, ticker: str, price: Decimal | None,
           now: datetime | None = None) -> dict[str, Decimal]:
    try:
        bars = list(data.get_bars(ticker, days=40))
    except Exception:  # noqa: BLE001 — the move is optional context
        return {}
    closes = [Decimal(str(b.close)) for b in bars if getattr(b, "close", None)]
    if not closes or price is None:
        return {}
    out = {}
    # The last bar may be today's (still forming) or yesterday's close.
    stamp = getattr(bars[-1], "ts", None)
    try:
        is_today = stamp.date() == (now or datetime.now(UTC)).date()
    except Exception:  # noqa: BLE001 — an odd timestamp: treat the bar as history
        is_today = False
    history = closes[:-1] if is_today and len(closes) > 1 else closes
    out["day_pct"] = _pct(price, history[-1])
    if len(history) >= 5:
        out["d5_pct"] = _pct(price, history[-5])
    if len(history) >= 20:
        out["d20_pct"] = _pct(price, history[-20])
    return out


def consult(ticker: str, side: str, *, qty: Any = None, notional: Any = None,
            price: Any = None, broker: Any, data: Any, strategies: Any,
            views: Any = None, max_position_weight: Any = None,
            min_cash_reserve: Any = None, now: datetime | None = None) -> Consult:
    """The pre-trade facts for one intended trade. Reads only."""
    from allpath_trade.strategy.model import RuleState, StrategyStatus

    now = now or datetime.now(UTC)
    ticker = ticker.strip().upper()
    side = side.strip().lower()
    c = Consult(ticker=ticker, side=side, qty=_dec(qty), price=_dec(price), value=None,
                min_cash_reserve=_dec(min_cash_reserve) or Decimal(0))
    if c.price is not None:
        c.price_source = "the owner's price"
    else:
        try:
            c.price = data.get_quote(ticker).price
            c.price_source = "current quote"
        except Exception as exc:  # noqa: BLE001 — a consult without a price still helps
            c.notes.append(f"no quote for {ticker} ({exc}); size and distance checks skipped")
    amount = _dec(notional)
    if c.qty is None and amount is not None and c.price:
        c.qty = (amount / c.price).quantize(Decimal("0.0001"))
    if c.qty is not None and c.price:
        c.value = c.qty * c.price
    elif amount is not None:
        c.value = amount

    # account
    try:
        account = broker.get_account()
        positions = {p.ticker: p for p in broker.get_positions()}
        c.equity, c.cash_before = account.equity, account.cash
        held = positions.get(ticker)
        if held is not None:
            c.held_qty, c.avg_cost = held.qty, held.avg_entry_price
        if c.equity and c.equity > 0 and c.price:
            c.weight_before = c.held_qty * c.price / c.equity
            if c.value is not None:
                sign = 1 if side == "buy" else -1
                c.weight_after = max(Decimal(0),
                                     (c.held_qty * c.price + sign * c.value) / c.equity)
        if c.value is not None:
            c.cash_after = c.cash_before - c.value if side == "buy" else c.cash_before + c.value
    except Exception as exc:  # noqa: BLE001
        c.notes.append(f"ledger unavailable ({exc})")

    # plan
    docs = []
    try:
        docs = [d for d in strategies.load_all(status=None, errors=[])
                if d.position.ticker == ticker]
    except Exception as exc:  # noqa: BLE001
        c.notes.append(f"strategies unavailable ({exc})")
    active = [d for d in docs if d.status == StrategyStatus.ACTIVE]
    if active:
        doc = active[0]
        plan = doc.position
        c.plan = {"strategy_id": doc.id, "thesis": " ".join(doc.thesis.split())[:300],
                  "horizon": doc.horizon.value if doc.horizon else "",
                  "bias": doc.bias.value if doc.bias else "",
                  "invalidation": " ".join(doc.review.invalidation.split())[:200],
                  "target_weight": plan.target_weight, "max_weight": plan.max_weight}
        limits = [w for w in (plan.max_weight, _dec(max_position_weight)) if w is not None]
        c.weight_limit = min(limits) if limits else None
    else:
        c.weight_limit = _dec(max_position_weight)
        if docs:
            c.notes.append("a strategy exists for this name but is not active: "
                           + ", ".join(f"{d.id} ({d.status.value})" for d in docs))
    ticker_views = False
    if views is not None:
        try:
            from allpath_trade.agent.view_tools import format_view

            for v in views.active(now):
                if v["subject"] in (ticker, "MARKET", VISION_SUBJECT):
                    c.views.append(format_view(v))
                    ticker_views = ticker_views or v["subject"] == ticker
        except Exception as exc:  # noqa: BLE001
            c.notes.append(f"views unavailable ({exc})")
    for doc in active:
        c.levels += price_levels(doc, c.price)
    c.moves = _moves(data, ticker, c.price, now)

    # impulse flags
    def flag(code: str, text: str) -> None:
        c.flags.append({"code": code, "text": text})

    if c.plan is None and not ticker_views:
        flag("no_plan", f"no active strategy and no recorded view for {ticker}: "
                        "there is no written plan to check this against")
    day = c.moves.get("day_pct")
    if day is not None:
        if side == "buy" and day >= CHASE_DAY_PCT:
            flag("chasing", f"{ticker} is up {day}% today: buying now is chasing the move")
        if side == "sell" and day <= -CHASE_DAY_PCT:
            flag("chasing", f"{ticker} is down {abs(day)}% today: selling now is selling "
                            "into the drop")
    stops = [lv for lv in c.levels if lv["kind"] == "stop"]
    if side == "buy" and c.price:
        below = [lv for lv in stops if c.price < lv["level"]]
        if below:
            lv = max(below, key=lambda e: e["level"])
            flag("below_own_stop", f"the price ({_money(c.price)}) is below your own stop "
                                   f"{lv['strategy_id']}/{lv['rule_id']} at "
                                   f"{_money(lv['level'])}: this adds under the stop")
        recent = None
        for doc in active:
            for rule in doc.rules:
                if not any(lv["rule_id"] == rule.id and lv["strategy_id"] == doc.id
                           for lv in stops):
                    continue
                try:
                    fired = strategies.last_fire(doc.id, rule.id)
                except Exception:  # noqa: BLE001
                    fired = None
                if fired is None:
                    continue
                if fired.tzinfo is None:
                    fired = fired.replace(tzinfo=UTC)
                if now - fired <= timedelta(days=REBUY_COOLDOWN_DAYS) and (
                        recent is None or fired > recent[0]):
                    recent = (fired, doc.id, rule.id, rule.state == RuleState.ARMED)
        if recent is not None:
            flag("rebuy_after_stop", f"your stop {recent[1]}/{recent[2]} fired on "
                                     f"{recent[0].date().isoformat()}, less than "
                                     f"{REBUY_COOLDOWN_DAYS} days ago: this is re-buying "
                                     "right after a stop-out")
    if (side == "buy" and c.weight_after is not None and c.weight_limit is not None
            and c.weight_after > c.weight_limit):
        flag("oversize", f"{ticker} would be {c.weight_after:.1%} of the account, over the "
                         f"{c.weight_limit:.1%} limit")
    if side == "buy" and c.cash_after is not None and c.cash_after < c.min_cash_reserve:
        flag("cash_short", f"cash after the trade would be {_money(c.cash_after)}"
                           + (f", under the {_money(c.min_cash_reserve)} reserve"
                              if c.min_cash_reserve > 0 else ": the ledger has "
                              f"{_money(c.cash_before)} in cash"))
    if side == "sell" and c.qty is not None and c.qty > c.held_qty:
        flag("oversell", f"the ledger holds {_plain(c.held_qty)} {ticker}, less than the "
                         f"{_plain(c.qty)} to sell (sync the holdings first if it is wrong)")
    return c


def _money(value: Any) -> str:
    from allpath_trade.compact_cards import money

    return money(value)


def _plain(value: Decimal) -> str:
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_consult(c: Consult) -> str:
    """The tool result the chat model reads (English; the model writes the
    reply in the account's language)."""
    size = (f"{_plain(c.qty)} sh" if c.qty is not None else "size not given")
    at = f" @ {_money(c.price)} ({c.price_source})" if c.price else ""
    value = f" = {_money(c.value.quantize(Decimal('0.01')))}" if c.value is not None else ""
    head = (f"PRE-TRADE CHECK #{c.consult_id or '-'}: {c.side.upper()} {c.ticker} "
            f"{size}{at}{value}")
    lines = [head, f"VERDICT FLOOR: {c.floor.upper()} (you may be stricter, never looser)"]
    if c.flags:
        lines.append("IMPULSE / RISK FLAGS:")
        lines += [f"- {f['code']}: {f['text']}" for f in c.flags]
    else:
        lines.append("IMPULSE / RISK FLAGS: none raised by the code checks")
    if c.plan:
        p = c.plan
        lines.append(f"PLAN: strategy {p['strategy_id']}"
                     + (f" ({p['horizon']}, {p['bias']})" if p["horizon"] or p["bias"] else "")
                     + f": {p['thesis'] or '(no thesis text)'}")
        if p["invalidation"]:
            lines.append(f"  invalidation: {p['invalidation']}")
        if p["target_weight"] is not None or p["max_weight"] is not None:
            lines.append("  target weight "
                         + (f"{p['target_weight']:.1%}" if p["target_weight"] is not None
                            else "-")
                         + ", max weight "
                         + (f"{p['max_weight']:.1%}" if p["max_weight"] is not None else "-"))
    else:
        lines.append("PLAN: no active strategy for this name")
    lines.append("OWNER'S VIEWS: " + ("; ".join(c.views) if c.views else "none on file"))
    if c.equity is not None:
        held = (f"{_plain(c.held_qty)} sh, avg {_money(c.avg_cost)}" if c.held_qty
                else "none held")
        weights = ""
        if c.weight_before is not None:
            weights = f"; weight {c.weight_before:.1%}"
            if c.weight_after is not None:
                weights += f" -> {c.weight_after:.1%} after"
            if c.weight_limit is not None:
                weights += f" (limit {c.weight_limit:.1%})"
        lines.append(f"POSITION: {held}{weights}")
        cash = f"CASH: {_money(c.cash_before)}"
        if c.cash_after is not None:
            cash += f" -> {_money(c.cash_after)} after"
        lines.append(cash)
    if c.levels:
        lines.append("YOUR LEVELS FOR THIS NAME:")
        for lv in sorted(c.levels, key=lambda e: e["level"]):
            dist = (f", price is {lv['distance_pct']:+}% from it"
                    if "distance_pct" in lv else "")
            lines.append(f"- {lv['kind']} {lv['strategy_id']}/{lv['rule_id']}: price "
                         f"{lv['op']} {_money(lv['level'])} -> {lv['action']} "
                         f"[{lv['state']}]{dist}")
    else:
        lines.append("YOUR LEVELS FOR THIS NAME: none (no stop, entry or take-profit rule)")
    if c.moves:
        labels = {"day_pct": "today", "d5_pct": "5 sessions", "d20_pct": "20 sessions"}
        lines.append("RECENT MOVE: " + ", ".join(
            f"{labels[k]} {v:+}%" for k, v in c.moves.items()))
    else:
        lines.append("RECENT MOVE: not available")
    lines.append("UPCOMING EVENTS: not in the data tools; check the earnings date and known "
                 "catalysts with web_search, and say so if you could not.")
    lines += [f"NOTE: {note}" for note in c.notes]
    return "\n".join(lines)


def log_consult(conn: Any, c: Consult, *, account: str, conversation_id: int | None,
                now: datetime | None = None) -> int | None:
    now = now or datetime.now(UTC)
    try:
        cur = conn.execute(
            "INSERT INTO trade_consults (ts, account, ticker, side, qty, price, value,"
            " flags, floor, conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(), account, c.ticker, c.side,
             None if c.qty is None else str(c.qty),
             None if c.price is None else str(c.price),
             None if c.value is None else str(c.value),
             json.dumps(c.flag_codes), c.floor, conversation_id))
        conn.commit()
        return int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001 — a log row never blocks the advice
        print(f"[advisor] consult log failed: {exc}", file=sys.stderr)
        return None


# --- recording a real trade -------------------------------------------------

i18n.register({
    "Record real trade: {side} {qty} {ticker} @ {price}":
        "记录真实成交：{side} {qty} {ticker} @ {price}",
    "Total {value}": "合计 {value}",
    "Position: {old} → {new} sh": "持仓：{old} → {new} 股",
    "Cash: {old} → {new}": "现金：{old} → {new}",
    "Confirm books it in the shadow ledger at your price. The app places no order.":
        "确认后按你的成交价记入 shadow 账本。app 不会下任何订单。",
    "{prefix}[AllPath] {ticker}: confirm your real trade": "{prefix}[AllPath] {ticker}：请确认你的真实成交",
    # act-prompt cards
    "Your move: place this at your broker. Then tap Done (books it at {price}), "
    "or reply to this message with your real fill price.":
        "轮到你了：请在券商下这笔单。然后点「已成交」（按 {price} 记账），或者回复本条写你的实际成交价。",
    "Your move: place this at your broker, then tap Done.":
        "轮到你了：请在券商下这笔单，然后点「已成交」。",
    "✅ Done @ {price}": "✅ 已成交 @ {price}",
    "✅ Done": "✅ 已成交",
    "⏭ Skipped": "⏭ 没做",
    "💬 Discuss": "💬 讨论",
    "{prefix}✅ Booked #{review_id}: {side} {qty} {ticker} @ {price} in your shadow ledger.":
        "{prefix}✅ 已记账 #{review_id}：{side} {qty} {ticker} @ {price}，已写入 shadow 账本。",
    " Fill different? Reply to the card with your price.": " 成交价不一样？回复卡片写上你的价格。",
    "{prefix}✅ #{review_id} corrected: fill price is now {price}.":
        "{prefix}✅ #{review_id} 已更正：成交价改为 {price}。",
    "{prefix}⚠️ #{review_id} was booked at the quote; the price could not be set to "
    "{price}: {error}": "{prefix}⚠️ #{review_id} 已按报价记账，但没能把成交价改成 {price}：{error}",
    "{prefix}⏭ Skipped #{review_id}. The ledger is unchanged.":
        "{prefix}⏭ 已跳过 #{review_id}，账本没有变化。",
    "Booked": "已记账", "Skipped": "已跳过", "Discuss": "讨论",
    "Let's talk it through.": "我们聊聊这笔。",
})


def preview_real_fill(before: dict[str, Any], *, ticker: str, side: str, qty: Decimal,
                      price: Decimal) -> dict[str, Any]:
    """`full_snapshot` after a real fill, or ValueError when the ledger
    cannot hold it (selling more than it has, or not enough cash)."""
    positions = {t: dict(p) for t, p in (before.get("positions") or {}).items()}
    cash = Decimal(str(before.get("cash", "0")))
    held = positions.get(ticker)
    held_qty = Decimal(held["qty"]) if held else Decimal(0)
    if side == "buy":
        cost = qty * price
        if cost > cash:
            raise ValueError(
                f"the ledger has {_money(cash)} in cash, less than the {_money(cost)} this "
                "buy cost; sync your holdings (or set cash) first, then record it")
        old_avg = Decimal(held["avg_cost"]) if held else Decimal(0)
        new_qty = held_qty + qty
        new_avg = ((held_qty * old_avg + cost) / new_qty).quantize(Decimal("0.000001"))
        positions[ticker] = {"qty": str(new_qty), "avg_cost": str(new_avg)}
        cash -= cost
    else:
        if qty > held_qty:
            raise ValueError(
                f"the ledger holds {_plain(held_qty)} {ticker}, less than the {_plain(qty)} "
                "sold; sync your holdings first, then record it")
        remaining = held_qty - qty
        if remaining == 0:
            positions.pop(ticker, None)
        else:
            positions[ticker] = {"qty": str(remaining), "avg_cost": held["avg_cost"]}
        cash += qty * price
    return {"positions": positions, "cash": str(cash)}


def apply_real_fill(ledger: Any, conn: Any, args: dict[str, Any]) -> None:
    """The approved real trade: booked at the owner's price and quantity.
    The caller (`apply_shadow_edit_factory`) has already checked staleness."""
    from allpath_trade.agent.shadow_tools import _parse_money

    qty = _parse_money(args["qty"], field="qty")
    price = _parse_money(args["price"], field="price")
    with conn.transaction():
        ledger.record_real_fill(args["ticker"], args["side"], qty, price)
        consult_id = args.get("consult_id")
        if consult_id:
            conn.execute("UPDATE trade_consults SET outcome = 'recorded' WHERE id = ?",
                         (int(consult_id),))


def register_advisor_tools(registry: ToolRegistry, *, ledger: Any, data: Any, queue: Any,
                           strategies: Any, views: Any, conn: Any, settings: Any,
                           conversation_id_fn: Any, account: str = "shadow",
                           notifier: Any = None, web_base_url: str = "",
                           app_state: Any = None, telegram_bot_token: str = "") -> None:
    """`pre_trade_check` and `record_real_trade` for the shadow chat registry."""
    from allpath_trade.agent.shadow_tools import _parse_money, _valid_ticker, full_snapshot
    from allpath_trade.notify import events
    from allpath_trade.notify.dispatch import notify_review_queued
    from allpath_trade.risk.gate import limits_for_account

    def _side(raw: str) -> str | None:
        side = str(raw or "").strip().lower()
        return side if side in ("buy", "sell") else None

    def pre_trade_check(ticker: str, side: str, qty: str = "", notional: str = "",
                        price: str = "") -> str:
        symbol, side_ok = _valid_ticker(ticker), _side(side)
        if symbol is None:
            return f"error: invalid ticker {ticker!r}"
        if side_ok is None:
            return "error: side must be buy or sell"
        limits = limits_for_account(settings, account)
        result = consult(symbol, side_ok, qty=qty, notional=notional, price=price,
                         broker=ledger, data=data, strategies=strategies, views=views,
                         max_position_weight=limits.max_position_weight,
                         min_cash_reserve=limits.min_cash_reserve)
        result.consult_id = log_consult(conn, result, account=account,
                                        conversation_id=conversation_id_fn())
        return render_consult(result)

    def record_real_trade(ticker: str, side: str, qty: str, price: str,
                          consult_id: str = "") -> str:
        lang = i18n.of(queue)
        symbol, side_ok = _valid_ticker(ticker), _side(side)
        if symbol is None:
            return f"error: invalid ticker {ticker!r}"
        if side_ok is None:
            return "error: side must be buy or sell"
        try:
            qty_d = _parse_money(str(qty).replace(",", ""), field="qty")
            price_d = _parse_money(str(price).replace(",", "").replace("$", ""),
                                   field="price")
        except ValueError as exc:
            return f"error: {exc}"
        if qty_d <= 0 or price_d <= 0:
            return "error: qty and price must be greater than 0 (his real fill)"
        before = full_snapshot(ledger)
        try:
            after = preview_real_fill(before, ticker=symbol, side=side_ok, qty=qty_d,
                                      price=price_d)
        except ValueError as exc:
            return f"NOT QUEUED: {exc}."
        held_before = (before["positions"].get(symbol) or {}).get("qty", "0")
        held_after = (after["positions"].get(symbol) or {}).get("qty", "0")
        title = tr(lang, "Record real trade: {side} {qty} {ticker} @ {price}",
                   side=i18n.word(lang, side_ok.capitalize()) if lang != "en"
                   else side_ok.upper(), qty=_plain(qty_d), ticker=symbol,
                   price=_money(price_d))
        consult_ref = int(consult_id) if str(consult_id).strip().isdigit() else None
        try:
            rid = queue.add_shadow_edit(
                op=OP, ticker=symbol, action=title,
                args={"ticker": symbol, "side": side_ok, "qty": str(qty_d),
                      "price": str(price_d), "consult_id": consult_ref},
                before=before, after=after, conversation_id=conversation_id_fn())
        except Exception as exc:  # noqa: BLE001
            return f"error: could not queue the trade record: {exc}"
        body = "\n".join([
            title,
            tr(lang, "Total {value}", value=_money((qty_d * price_d).quantize(Decimal("0.01")))),
            tr(lang, "Position: {old} → {new} sh", old=_plain(Decimal(held_before)),
               new=_plain(Decimal(held_after))),
            tr(lang, "Cash: {old} → {new}", old=_money(before["cash"]),
               new=_money(Decimal(after["cash"]).quantize(Decimal("0.01")))),
            "",
            tr(lang, "Confirm books it in the shadow ledger at your price. "
                     "The app places no order.")])
        try:
            subject, _classic = events.review_queued(
                account=account, review_id=rid, ticker=symbol, action=title,
                strategy_id="", kind="shadow_edit", **i18n.lang_kw(queue))
            notify_review_queued(
                queue=queue, notifier=notifier, app_state=app_state,
                telegram_bot_token=telegram_bot_token, review_id=rid, subject=subject,
                body=i18n.Localized(body, lang) if lang != "en" else body, account=account)
        except Exception as exc:  # noqa: BLE001 — the row is queued; a card is best-effort
            print(f"[advisor] card for #{rid} failed: {exc}", file=sys.stderr)
        return (f"QUEUED as #{rid}: one tap on the card (or the Pending page) books it at "
                f"his price. The ledger is unchanged until then.\n{body}")

    s = {"type": "string"}
    registry.register(
        "pre_trade_check",
        "MUST be called first whenever the owner says he wants to buy or sell something "
        "in his real account, before any opinion or proposal. Returns the facts computed "
        "in code: plan fit, size and cash after the trade, distance to his own stop / "
        "entry levels, the recent move, impulse flags, and a verdict floor (go / wait / "
        "dont). Changes nothing.",
        {"type": "object", "properties": {
            "ticker": s, "side": {"type": "string", "enum": ["buy", "sell"]},
            "qty": {"type": "string", "description": "Shares, if he said."},
            "notional": {"type": "string", "description": "Dollar amount, if he gave "
                                                           "that instead of shares."},
            "price": {"type": "string", "description": "His intended price, if any; "
                                                        "else the current quote is used."}},
         "required": ["ticker", "side"]},
        pre_trade_check)
    registry.register(
        "record_real_trade",
        "Record a trade the owner TELLS you he has actually made at his broker, at HIS "
        "fill price and quantity (never a quote, never an estimate). Queues one "
        "confirmation card; the ledger changes only when he confirms.",
        {"type": "object", "properties": {
            "ticker": s, "side": {"type": "string", "enum": ["buy", "sell"]},
            "qty": {"type": "string", "description": "Shares actually filled."},
            "price": {"type": "string", "description": "His actual fill price per share."},
            "consult_id": {"type": "string",
                           "description": "The PRE-TRADE CHECK # this follows, if any."}},
         "required": ["ticker", "side", "qty", "price"]},
        record_real_trade)


# --- act-prompt cards -------------------------------------------------------

def act_prompts(queue: object) -> bool:
    """Whether the account behind `queue` has ACT_PROMPT_CARDS on (app.py
    sets `ReviewQueue.act_prompts`); anything else counts as off."""
    return getattr(queue, "act_prompts", False) is True


def act_line(price: Any, lang: str = "en") -> str:
    """The card's last line in place of "approving only records it"."""
    if _dec(price):
        return tr(lang, "Your move: place this at your broker. Then tap Done (books it "
                        "at {price}), or reply to this message with your real fill price.",
                  price=_money(price))
    return tr(lang, "Your move: place this at your broker, then tap Done.")


def is_act_row(queue: object, row: Any) -> bool:
    """A rule-triggered stock order on an account with act prompts on."""
    try:
        return (act_prompts(queue) and row["kind"] == "order"
                and row["source"] == "sentinel" and row["account"] == "shadow")
    except (KeyError, IndexError, TypeError):
        return False


def trigger_price(row: Any) -> Decimal | None:
    """The price the rule fired at, from the review's stored snapshot."""
    try:
        return _dec(json.loads(row["snapshot"]).get("price"))
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        return None


def act_keyboard(review_id: int, nonce: str, price: Decimal | None,
                 lang: str = "en") -> dict[str, Any]:
    done = (tr(lang, "✅ Done @ {price}", price=_money(price)) if price
            else tr(lang, "✅ Done"))
    return {"inline_keyboard": [[
        {"text": done, "callback_data": f"rv:approve:{review_id}:{nonce}"},
        {"text": tr(lang, "⏭ Skipped"), "callback_data": f"rv:reject:{review_id}:{nonce}"},
        {"text": tr(lang, "💬 Discuss"), "callback_data": f"rv:discuss:{review_id}:{nonce}"},
    ]]}


_PRICE_REPLY = re.compile(
    r"^\s*(?:done|filled|sold|bought|成交|已成交|已卖|已买|成交价)?\s*(?:@|at|在|：|:)?\s*"
    r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:美元|块)?\s*[.。]?\s*$", re.IGNORECASE)


def price_from_reply(text: str) -> Decimal | None:
    """`135.10`, `done 135.1`, `成交 135.1`, `@ $135.10` -> the price; any
    other text (a question, a sentence) -> None."""
    match = _PRICE_REPLY.match(text or "")
    value = _dec(match.group(1)) if match else None
    return value if value and value > 0 else None


def book_fill_price(ledger: Any, order_id: Any, price: Decimal) -> None:
    """Set a just-recorded shadow order's fill to the owner's price."""
    ledger.record_fill(str(order_id), price)
