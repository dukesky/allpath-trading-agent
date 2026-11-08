"""Offline fixture environment for the red-team harness.

`RedteamEnv` builds the system through the PRODUCTION assembly,
`allpath_trade.app.build_components`, on a temp directory. Only the external
boundaries are swapped for fakes:

- broker of record (paper)  -> FakeBroker (in-memory fills at the fake quote)
- market data               -> FakeData (fixed quotes; unknown ticker raises)
- every LLM tier            -> ScriptedLLM (recorded/scripted replies)
- web search                -> canned results (the attack payloads)
- notifier                  -> RecordingNotifier
- the clock                 -> frozen; US market treated as open

Everything else (risk gate, executor, review queue, revision applier,
sentinel, regime choke point, memory guard, tool registries, reflection,
consolidation) is the real code. Settings are built with `_env_file=None`
and every credential is overridden, so no secret from the shell or a `.env`
can reach the harness, and nothing here can talk to Alpaca, an LLM
provider, Telegram or the Air.
"""

from __future__ import annotations

import contextlib
import itertools
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from allpath_trade.broker.base import (
    Account,
    Broker,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)
from allpath_trade.config import Settings
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.llm.base import LLMClient, LLMResponse, ToolCall
from allpath_trade.notify.base import Notifier

# The corpus row's `regime` (human/auto), applied by every env the row's
# runner builds unless the runner sets another value itself. Set by
# runner.run_case; None (the default) stores no regime at all.
DEFAULT_REGIME: ContextVar[str | None] = ContextVar("redteam_regime", default=None)

FROZEN_NOW = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)  # 11:00 ET, a Tuesday
DEFAULT_QUOTES = {"AAPL": "200", "NVDA": "190", "MSFT": "400", "TQQQ": "80",
                  "GME": "25"}


class FakeData(DataSource):
    def __init__(self, quotes: dict[str, str] | None = None) -> None:
        self.quotes = {k: Decimal(v) for k, v in (quotes or DEFAULT_QUOTES).items()}

    def get_quote(self, ticker: str) -> Quote:
        if ticker not in self.quotes:
            raise LookupError(f"no quote for {ticker}")
        return Quote(ticker=ticker, price=self.quotes[ticker], as_of=FROZEN_NOW)

    def get_bars(self, ticker: str, days: int = 365) -> list:
        return []


class FakeBroker(Broker):
    """Fills every market order at the fake quote (or $100 for a ticker with
    no quote, standing in for a broker that accepts any symbol). Positions
    and cash update, so a second order sees the first one's effect."""

    name = "fake"

    def __init__(self, data: FakeData, *, equity: str = "100000",
                 cash: str = "50000", positions: dict[str, str] | None = None,
                 is_paper: bool = True) -> None:
        self.data = data
        self.is_paper = is_paper
        self.cash = Decimal(cash)
        self.qty: dict[str, Decimal] = {k: Decimal(v) for k, v in (positions or {}).items()}
        # Whatever part of `equity` is neither cash nor a listed position
        # (other holdings), held constant so equity moves only with fills.
        self._other = Decimal(equity) - self.cash - self._held_value()
        self.submitted: list[OrderIntent] = []
        self._ids = itertools.count(1)
        self._orders: dict[str, Order] = {}

    def _price(self, ticker: str) -> Decimal:
        q = self.data.quotes.get(ticker)
        return q if q is not None else Decimal(100)

    def _held_value(self) -> Decimal:
        return sum((q * self._price(t) for t, q in self.qty.items()), Decimal(0))

    def get_account(self) -> Account:
        equity = self._other + self.cash + self._held_value()
        return Account(equity=equity, cash=self.cash, buying_power=self.cash)

    def get_positions(self) -> list[Position]:
        out = []
        for t, q in sorted(self.qty.items()):
            if q <= 0:
                continue
            p = self._price(t)
            out.append(Position(ticker=t, qty=q, avg_entry_price=p,
                                market_value=q * p, unrealized_pl=Decimal(0)))
        return out

    def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    def get_orders(self, open_only: bool = True) -> list[Order]:
        return []

    def submit_order(self, intent: OrderIntent) -> Order:
        self.submitted.append(intent)
        price = self._price(intent.ticker)
        qty = intent.qty if intent.qty is not None else (intent.notional / price)
        sign = 1 if intent.side == OrderSide.BUY else -1
        self.qty[intent.ticker] = self.qty.get(intent.ticker, Decimal(0)) + sign * qty
        self.cash -= sign * qty * price
        oid = f"fake-{next(self._ids)}"
        order = Order(id=oid, ticker=intent.ticker, side=intent.side, qty=qty,
                      notional=intent.notional, status=OrderStatus.FILLED,
                      filled_qty=qty, filled_avg_price=price,
                      submitted_at=FROZEN_NOW, filled_at=FROZEN_NOW)
        self._orders[oid] = order
        return order

    def cancel_order(self, order_id: str) -> None:
        return None


class ScriptedLLM(LLMClient):
    """Replays a script of replies and records every request it receives.
    An exhausted script answers with plain text, so a model that is asked
    more than the script expected degrades instead of crashing the row."""

    def __init__(self, tier: str) -> None:
        self.model = f"scripted-{tier}"
        self.tier = tier
        self.script: list[LLMResponse] = []
        self.requests: list[list[dict]] = []
        # Names of the tools offered on the most recent request: what this
        # agent's registry actually exposes (least-privilege evidence).
        self.offered: list[str] = []

    def complete(self, messages: list[dict], tools: Any = None) -> LLMResponse:
        self.requests.append([dict(m) for m in messages])
        self.offered = sorted(t.name for t in (tools or []))
        if not self.script:
            return LLMResponse(text="(script exhausted)")
        item = self.script.pop(0)
        if isinstance(item, Exception):  # a scripted provider failure
            raise item
        return item

    def tool_results(self) -> list[str]:
        """Every tool-result message this model was shown, in order."""
        seen: list[str] = []
        out: list[str] = []
        for req in self.requests:
            for m in req:
                if m.get("role") == "tool":
                    key = f"{m.get('tool_call_id')}:{m.get('content')}"
                    if key not in seen:
                        seen.append(key)
                        out.append(str(m.get("content")))
        return out


def call(name: str, args: dict, id_: str = "c1") -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=id_, name=name, arguments=args)],
                       stop_reason="tool_use")


def say(text: str) -> LLMResponse:
    return LLMResponse(text=text)


REPORT_TEXT = """\
REPORT
Day summary: reviewed the day.
Per-strategy check: done.
Lessons: none.
Proposals: see queue.
SUMMARY
Reviewed the day. Nothing else to add. One proposal may be queued. \
No lessons today. Nothing needs attention.
"""


@dataclass
class RecordingNotifier(Notifier):
    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, subject: str, body: str) -> bool:
        self.sent.append((subject, body))
        return True


class RedteamEnv:
    """Context manager: builds the components on enter, undoes every patch on
    exit. One env per attack row, so rows never see each other's state."""

    def __init__(self, root: Path, *, equity: str = "100000", cash: str = "50000",
                 positions: dict[str, str] | None = None, is_paper: bool = True,
                 quotes: dict[str, str] | None = None, **settings: Any) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.data = FakeData(quotes)
        self.broker = FakeBroker(self.data, equity=equity, cash=cash,
                                 positions=positions, is_paper=is_paper)
        self.notifier = RecordingNotifier()
        self.llms = {t: ScriptedLLM(t) for t in ("chat", "review", "memory")}
        self.search_results: list[dict] = []
        self.search_queries: list[str] = []
        self._settings_overrides = settings
        self._patches: list[tuple[Any, str, Any]] = []

    # -- patching ----------------------------------------------------------
    def _patch(self, obj: Any, name: str, value: Any) -> None:
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def __enter__(self) -> Self:
        from allpath_trade import app, sentinel
        from allpath_trade.agent import readonly_tools
        from allpath_trade.llm import factory

        try:
            self._patch(factory, "build_llm",
                        lambda settings, tier="chat", usage_store=None: self.llms[tier])
            self._patch(readonly_tools, "_default_search", self._search)
            self._patch(app, "YFinanceSource", lambda: self.data)
            self._patch(app, "build_notifier", lambda settings: self.notifier)
            self._patch(sentinel, "_now", lambda: FROZEN_NOW)
            self._patch(sentinel, "_us_market_open_now", lambda: True)
            self.settings = self.make_settings()
            self.c = app.build_components(self.settings, broker=self.broker)
        except BaseException:
            self._unpatch()
            raise
        self.paper = self.c.accounts["paper"]
        self.shadow = self.c.accounts["shadow"]
        if DEFAULT_REGIME.get() in ("human", "auto"):
            self.set_regime(DEFAULT_REGIME.get())
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.c.conn.close()
        except Exception:  # noqa: BLE001, S110 — teardown best effort
            pass
        self._unpatch()

    def _unpatch(self) -> None:
        while self._patches:
            obj, name, old = self._patches.pop()
            setattr(obj, name, old)

    def make_settings(self) -> Settings:
        base: dict[str, Any] = dict(  # noqa: C408 — keyword form reads as settings
            db_path=self.root / "redteam.db",
            strategies_dir=self.root / "strategies",
            memory_dir=self.root / "memory",
            # Credentials: all blank except a dummy LLM key, which only makes
            # the production assembly wire the LLM-backed agents (the
            # factory itself is patched, so the key is never sent anywhere).
            llm_provider="openrouter", openrouter_api_key="redteam-dummy",
            openai_api_key="", anthropic_api_key="",
            alpaca_api_key="", alpaca_secret_key="",
            telegram_bot_token="", smtp_host="", notify_to="", ntfy_url="",
            agent_webhook_url="", web_base_url="",
            options_trading=False, experiment_auto_apply_revisions=False,
            reflection_max_iters=8, context_budget_tokens=200000,
        )
        base.update(self._settings_overrides)
        return Settings(_env_file=None, **base)

    def _search(self, query: str, max_results: int = 5) -> list[dict]:
        self.search_queries.append(query)
        return list(self.search_results)

    # -- helpers the cases use ----------------------------------------------
    def set_regime(self, value: str | None, account: str = "paper") -> None:
        """Set the stored regime value directly (setup only -- the real
        `set_regime` is itself under test in the D rows)."""
        key = f"regime:{account}"
        if value is None:
            self.c.conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
            self.c.conn.commit()
        else:
            self.c.app_state.set(key, value)

    def write_strategy(self, strategy_id: str, text: str, account: str = "paper") -> Path:
        store = self.c.accounts[account].strategies
        store.directory.mkdir(parents=True, exist_ok=True)
        path = store.directory / f"{strategy_id}.yaml"
        path.write_text(text)
        return path

    def script(self, tier: str, *responses: LLMResponse) -> ScriptedLLM:
        """Load a fresh script; also forgets earlier requests, so each
        scripted episode's tool results stand alone."""
        llm = self.llms[tier]
        llm.script = list(responses)
        llm.requests = []
        llm.offered = []
        return llm

    @contextlib.contextmanager
    def web(self) -> Iterator[Any]:
        """A logged-in TestClient on the real web app (`web.app.create_app`)
        over this env's DB, for checking what the human actually sees. Built
        inside the env's patches, so it gets the same fakes."""
        from fastapi.testclient import TestClient

        from allpath_trade.web.app import create_app
        from allpath_trade.web.routes import dashboard as dashboard_route
        from allpath_trade.web.setup_status import SETUP_DISMISSED_KEY

        dashboard_route._quote_cache.clear()
        dashboard_route._equity_history_cache.clear()
        self.c.app_state.set(SETUP_DISMISSED_KEY, "1")
        settings = self.make_settings().model_copy(update={"web_token": "redteam-web"})
        with TestClient(create_app(settings, broker=self.broker)) as client:
            client.post("/login", data={"token": "redteam-web"})
            yield client
        dashboard_route._quote_cache.clear()
        dashboard_route._equity_history_cache.clear()

    def scrub(self, text: str) -> str:
        """Evidence must never carry temp paths (determinism, and the paper
        table shows behaviour, not this machine's layout)."""
        return text.replace(str(self.root.resolve()), "<tmp>").replace(str(self.root), "<tmp>")
