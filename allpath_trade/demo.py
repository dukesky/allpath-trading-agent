"""`allpath-trade demo`: a no-keys tour of the web UI on seeded sample data.

A first-time visitor usually has neither Alpaca nor LLM keys, and a fresh
`serve` without them shows nothing but empty pages. The demo answers "what
does this look like when it is running?" in one command, without an account
anywhere:

* it runs in its OWN working directory (a fresh temp dir by default), so its
  `.env`, SQLite DB, `strategies/` and `memory/` never touch a real install;
* paper's broker is `DemoBroker` (static positions, simulated fills: nothing
  is sent anywhere) and market data is `DemoData` (fixed sample prices, no
  network);
* no scheduler, no Telegram poller, and every outbound channel (Alpaca,
  Telegram, email, ntfy, webhooks, publish) is scrubbed from the process
  environment first, so a demo started from a shell that exports real keys
  still cannot reach them.

LLM keys in the environment are left alone on purpose: with one, the chat
page talks to a real model against the sandboxed demo data; without one it
runs on a small scripted stand-in (allpath_trade/demo_chat.py) that answers
a few sample questions through the real tool loop.
"""

from __future__ import annotations

import difflib
import ipaddress
import json
import math
import os
import secrets
import sqlite3
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from allpath_trade.broker.base import (
    Account,
    Broker,
    BrokerError,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)
from allpath_trade.data.base import Bar, DataSource, Quote

DEMO_PORT = 8790
DEMO_TOKEN = "demo"
SEEDED_KEY = "demo_seeded"

# Settings fields that reach anything outside this process. Matched
# case-insensitively against the environment, the way pydantic-settings
# reads it. LLM keys are deliberately absent (see the module docstring).
OUTBOUND_ENV = (
    "alpaca_api_key", "alpaca_secret_key", "telegram_bot_token",
    "smtp_host", "smtp_user", "smtp_password", "smtp_from", "notify_to",
    "ntfy_url", "agent_webhook_url", "agent_webhook_token",
    "publish_url", "publish_token", "web_base_url", "web_token",
)

# Sample prices: illustrative, not market data.
DEMO_PRICES: dict[str, Decimal] = {
    "NVDA": Decimal("212.40"),
    "AAPL": Decimal("268.10"),
    "MSFT": Decimal("505.20"),
    "AMZN": Decimal("231.50"),  # not held: the keyless chat drafts a strategy on it
}
_PREV_CLOSE: dict[str, Decimal] = {
    "NVDA": Decimal("209.85"),
    "AAPL": Decimal("270.02"),
    "MSFT": Decimal("501.10"),
    "AMZN": Decimal("229.90"),
}
_HOLDINGS: dict[str, tuple[Decimal, Decimal]] = {  # ticker: (qty, avg entry)
    "NVDA": (Decimal(40), Decimal("188.25")),
    "AAPL": (Decimal(25), Decimal("241.60")),
}
DEMO_CASH = Decimal("86500.00")


def scrub_outbound_env(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Remove every outbound credential from `environ` (default: this
    process's own environment). Returns the removed names."""
    env = os.environ if environ is None else environ
    wanted = set(OUTBOUND_ENV)
    removed = [k for k in list(env) if k.lower() in wanted]
    for k in removed:
        del env[k]
    return removed


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def demo_token(host: str) -> str:
    """The well-known token is only ever used on a loopback bind. Anything
    reachable from another machine gets a random one."""
    return DEMO_TOKEN if is_loopback(host) else secrets.token_urlsafe(16)


class DemoData(DataSource):
    """Offline market data: fixed sample quotes and a deterministic daily
    bar series. Unknown tickers raise, like a real source would."""

    def get_quote(self, ticker: str) -> Quote:
        t = ticker.strip().upper()
        if t not in DEMO_PRICES:
            raise LookupError(f"demo data has no quote for {t}")
        return Quote(ticker=t, price=DEMO_PRICES[t], as_of=datetime.now(UTC),
                     previous_close=_PREV_CLOSE[t])

    def get_bars(self, ticker: str, days: int = 365) -> list[Bar]:
        t = ticker.strip().upper()
        last = float(self.get_quote(t).price)
        today = datetime.now(UTC).replace(hour=20, minute=0, second=0, microsecond=0)
        bars = []
        for i in range(days, 0, -1):
            close = last * (1 - 0.0006 * i + 0.03 * math.sin(i / 9))
            bars.append(Bar(ts=today - timedelta(days=i), open=close * 0.997,
                            high=close * 1.01, low=close * 0.99, close=close,
                            volume=1_000_000))
        return bars


class DemoBroker(Broker):
    """Static sample portfolio. Orders fill instantly at the sample price and
    are kept in memory only; positions do not move, so the screens stay the
    same from one visit to the next."""

    name = "demo"
    is_paper = True

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def get_account(self) -> Account:
        equity = DEMO_CASH + sum(p.market_value for p in self.get_positions())
        return Account(equity=equity, cash=DEMO_CASH, buying_power=DEMO_CASH)

    def get_positions(self) -> list[Position]:
        out = []
        for ticker, (qty, avg) in _HOLDINGS.items():
            value = qty * DEMO_PRICES[ticker]
            out.append(Position(ticker=ticker, qty=qty, avg_entry_price=avg,
                                market_value=value, unrealized_pl=value - qty * avg))
        return out

    def get_order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise BrokerError(f"demo broker has no order {order_id}") from None

    def get_orders(self, open_only: bool = True) -> list[Order]:
        orders = list(self._orders.values())
        return [] if open_only else orders  # every demo order fills at once

    def submit_order(self, intent: OrderIntent) -> Order:
        price = DEMO_PRICES.get(intent.ticker)
        if price is None:
            raise BrokerError(f"demo broker only trades {', '.join(DEMO_PRICES)}")
        qty = intent.qty if intent.qty is not None else (
            intent.notional / price).quantize(Decimal("0.0001"))
        now = datetime.now(UTC)
        order = Order(id=f"demo-{len(self._orders) + 1}", ticker=intent.ticker,
                      side=intent.side, qty=intent.qty, notional=intent.notional,
                      status=OrderStatus.FILLED, filled_qty=qty,
                      filled_avg_price=price, submitted_at=now, filled_at=now)
        self._orders[order.id] = order
        return order

    def cancel_order(self, order_id: str) -> None:
        self.get_order(order_id)  # filled already: nothing to cancel

    def get_equity_history(self, days: int) -> list[tuple[datetime, Decimal]]:
        end = self.get_account().equity
        start = Decimal(100000)
        today = datetime.now(UTC).replace(hour=20, minute=0, second=0, microsecond=0)
        total = 200
        points = []
        for i in range(total, -1, -1):
            frac = Decimal(total - i) / total
            wiggle = Decimal(str(round(
                700 * math.sin(i / 11) + 380 * math.sin(i / 3.1)
                + 160 * math.sin(i * 1.7), 2)))
            value = start + (end - start) * frac + (wiggle if i else Decimal(0))
            points.append((today - timedelta(days=i), value))
        return points[-(days + 1):] if days >= 0 else []


# ---------------------------------------------------------------- seed data

NVDA_YAML = """\
id: nvda-ai-infra
name: NVDA - AI infrastructure core position
status: active
version: 3
authorization: confirm
thesis: >
  Core holding on data-center AI demand. Build toward a 12% weight on
  pullbacks, never chase strength. Invalidation: two straight quarters of
  data-center growth below 15%, or a lasting share loss to custom silicon.
position:
  ticker: NVDA
  target_weight: '0.12'
  max_weight: '0.15'
rules:
- id: add-on-pullback
  type: soft
  condition: price < 215 and position_weight < 0.12
  action: buy $2000
  state: armed
- id: take-profit
  type: soft
  condition: pnl_pct > 35
  action: sell 30%
  state: armed
- id: stop-loss
  type: hard
  condition: price < 170
  action: sell all
  state: armed
review:
  cadence: weekly
  invalidation: Data-center growth below 15% for two consecutive quarters
"""

AAPL_YAML = """\
id: aapl-services
name: AAPL - services margin swing
status: active
version: 2
authorization: notify
thesis: >
  Services revenue keeps lifting gross margin. Hold through the product
  cycle, take half off into strength, exit on a break of the summer low.
position:
  ticker: AAPL
  target_weight: '0.07'
  max_weight: '0.10'
rules:
- id: take-profit
  type: hard
  condition: price > 290
  action: sell 50%
  state: armed
- id: stop-loss
  type: hard
  condition: price < 225
  action: sell all
  state: armed
review:
  cadence: weekly
"""

MSFT_YAML = """\
id: msft-dip-buyer
name: MSFT - buy the next dip (draft)
status: draft
version: 1
authorization: confirm
thesis: >
  Drafted with the agent in chat: wait for a pullback under 480 before
  opening a starter position. Not live until you activate it.
position:
  ticker: MSFT
  target_weight: '0.05'
rules:
- id: starter-entry
  type: soft
  condition: position_weight == 0 and price < 480
  action: buy $3000
  state: armed
"""

STRATEGY_FILES = {
    "nvda-ai-infra": NVDA_YAML,
    "aapl-services": AAPL_YAML,
    "msft-dip-buyer": MSFT_YAML,
}

REPORT_BODY = """\
## Day summary
Sample report (demo data). Equity +0.4% on the day, led by NVDA (+1.2%).
One rule fired: **nvda-ai-infra / add-on-pullback** (price 212.40 < 215,
weight 8.3% < 12%). The strategy is `confirm`, so the order is waiting on
the Pending page instead of executing.

## Per-strategy check
- **NVDA - AI infrastructure core position:** thesis intact; the pullback
  rule fired as designed.
- **AAPL - services margin swing:** up 11% from entry. The stop at 225 now
  sits 16% below price, which gives back most of the gain.

## Lessons
- A fixed stop set at entry drifts out of date after a big move. Re-check
  stops whenever a position is more than 10% in profit.

## Proposed revisions
- **aapl-services:** raise the stop from 225 to 245. Queued on Pending for
  your approval (nothing changes until you approve it).
"""


def _seed_strategies(strategies_root: Path) -> None:
    paper = strategies_root / "paper"
    paper.mkdir(parents=True, exist_ok=True)
    for sid, text in STRATEGY_FILES.items():
        (paper / f"{sid}.yaml").write_text(text)


def seed_demo(conn: sqlite3.Connection, strategies_root: Path, memory_root: Path) -> bool:
    """Write the sample data into a demo working directory. Idempotent: a
    second call on the same DB is a no-op and returns False."""
    from allpath_trade.memory.store import MemoryStore
    from allpath_trade.risk.gate import RiskDecision
    from allpath_trade.store.app_state import AppState
    from allpath_trade.store.journal import TradeJournal
    from allpath_trade.store.reports import ReportStore
    from allpath_trade.store.reviews import ReviewQueue
    from allpath_trade.strategy.model import RuleState
    from allpath_trade.strategy.store import StrategyStore
    from allpath_trade.web.setup_status import SETUP_DISMISSED_KEY

    app_state = AppState(conn)
    if app_state.get(SEEDED_KEY):
        return False

    _seed_strategies(strategies_root)
    strategies = StrategyStore.for_account(strategies_root, conn, account="paper")

    journal = TradeJournal(conn, account="paper")
    ok = RiskDecision(approved=True)
    fills = [
        ("NVDA", Decimal(25), Decimal("181.40"), "nvda-ai-infra",
         "initial entry: price < 185 (approved by you)"),
        ("NVDA", Decimal(15), Decimal("199.66"), "nvda-ai-infra",
         "add-on-pullback (approved by you)"),
        ("AAPL", Decimal(25), Decimal("241.60"), "aapl-services",
         "entry after services beat (approved by you)"),
    ]
    for i, (ticker, qty, price, sid, reason) in enumerate(fills, start=1):
        intent = OrderIntent(ticker=ticker, side=OrderSide.BUY, qty=qty,
                             reason=reason, strategy_id=sid)
        ts = datetime.now(UTC) - timedelta(days=40 - 12 * i)
        order = Order(id=f"demo-seed-{i}", ticker=ticker, side=OrderSide.BUY,
                      qty=qty, notional=None, status=OrderStatus.FILLED,
                      filled_qty=qty, filled_avg_price=price,
                      submitted_at=ts, filled_at=ts)
        journal.record(intent, ok, order)

    queue = ReviewQueue(conn, executor=None, account="paper")
    price = DEMO_PRICES["NVDA"]
    order_intent = OrderIntent(ticker="NVDA", side=OrderSide.BUY,
                               notional=Decimal(2000),
                               reason="nvda-ai-infra/add-on-pullback",
                               strategy_id="nvda-ai-infra")
    handle = queue.add(
        strategy_id="nvda-ai-infra", rule_id="add-on-pullback", ticker="NVDA",
        rule_type="soft", condition="price < 215 and position_weight < 0.12",
        action="buy $2000",
        snapshot={"price": str(price), "position_weight": "0.0834",
                  "pnl_pct": "12.1"},
        intent=order_intent)
    queue.attach_analysis(int(handle), json.dumps({
        "recommendation": "approve",
        "reasoning": (
            "Sample analysis (demo data). The pullback is orderly (-4% from "
            "the high on falling volume) and nothing in the news flow touches "
            "the thesis. After this buy the weight is about 10%, still under "
            "the 12% target and the 15% cap. The risk gate pre-check passed."),
        "sources": [],
    }))
    # Sample decision brief, shown only with CARD_REASON_BRIEF on.
    queue.attach_brief(int(handle), {
        "why": "NVDA is under the 215 add trigger after an orderly -4% pullback.",
        "risk": "A deeper slide; the weight goes to about 10%, under the 15% cap.",
        "plan": "Fits the plan to add to this core holding only on pullbacks.",
        "origin": "demo"})
    strategies.set_rule_state("nvda-ai-infra", "add-on-pullback", RuleState.TRIGGERED)

    old = AAPL_YAML
    new = (AAPL_YAML.replace("version: 2", "version: 3")
           .replace("price < 225", "price < 245"))
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="aapl-services.yaml (current)",
        tofile="aapl-services.yaml (proposed)"))
    revision = queue.add_strategy_revision(
        strategy_id="aapl-services", ticker="AAPL", old_yaml=old, new_yaml=new,
        diff=diff, rationale=(
            "Sample proposal (demo data) from last night's reflection: AAPL is "
            "11% above entry, so the 225 stop would give back most of the "
            "gain. Raise it to 245."))
    queue.attach_brief(int(revision), {
        "why": "AAPL is 11% above entry, so the 225 stop gives back most of the gain.",
        "risk": "A stop at 245 sits closer to the price and can trigger on a normal dip.",
        "plan": "",
        "origin": "demo"})

    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    ReportStore(conn, account="paper").add(
        date=yesterday, body=REPORT_BODY,
        summary="Sample report: one confirm order and one stop revision await you",
        conversation_id=None, model="demo (sample data)", tokens_used=0)

    memory = MemoryStore(memory_root, conn, account="paper")
    memory.apply("profile", None, "add",
                 "Sample profile (demo data): long-term investor, 3-5 year "
                 "horizon, fine with 20% drawdowns, wants every buy confirmed.")
    memory.apply("stock", "NVDA", "add",
                 "Core AI-infrastructure holding. Adds only on pullbacks; "
                 "watch data-center growth each quarter.")
    memory.apply("strategy", "aapl-services", "add",
                 "Stop has not moved since entry; flagged by reflection.")
    memory.apply("lesson", "trailing-stops", "add",
                 "Re-check fixed stops once a position is more than 10% in profit.")

    app_state.set(SETUP_DISMISSED_KEY, "1")
    app_state.set(SEEDED_KEY, datetime.now(UTC).isoformat())
    return True

