# Phase 1: Foundation (Broker + Data + Risk + Executor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the safe-execution foundation of the allpath_trade package: config store, broker abstraction with Alpaca (paper) adapter, market data layer, deterministic risk gate, trade journal, order executor, and a `allpath-trade status` CLI to verify against a real Alpaca paper account.

**Architecture:** Sync Python core (mid/long-term trading needs no async). Every order flows OrderIntent → RiskGate (deterministic, cannot be bypassed) → Broker adapter (thin wrapper over official `alpaca-py`) → TradeJournal (SQLite). LLM never touches the broker directly — later phases only ever produce `OrderIntent`s and call `Executor.execute`.

**Tech Stack:** Python ≥3.11, uv (env/deps), pydantic v2 + pydantic-settings, alpaca-py, yfinance, sqlite3 (stdlib), pytest, ruff.

## Global Constraints

- Package name is `allpath_trade` (repo stays `allpath-trading-agent`).
- Python `>=3.11`; all core code is synchronous.
- Money is `Decimal`, never float (floats OK for OHLCV bars/weights).
- Paper-first: `RiskLimits.allow_live` defaults to `False`; AlpacaBroker defaults to `paper=True`.
- Market orders only in Phase 1 (daily-granularity mid/long-term trading; limit orders are a later phase).
- No short selling in Phase 1: sells must not exceed current position.
- Credentials live only in local `.env`, read/written through `SettingsStore`; real env vars override `.env`.
- Every module gets unit tests; deterministic money-path modules (risk, journal, executor) get exhaustive ones. Network-touching tests are marked `integration` and skipped without credentials.
- Run everything through `uv run` (e.g. `uv run pytest`).
- Commit after every task (at minimum); commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `allpath_trade/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces: installable `allpath_trade` package, `uv run pytest` green, `allpath_trade` console script stub target (wired in Task 9).

- [ ] **Step 1: Write files**

`pyproject.toml`:
```toml
[project]
name = "allpath_trade"
version = "0.1.0"
description = "All Path Trading Agent - an LLM-powered mid/long-term trading agent framework"
requires-python = ">=3.11"
dependencies = [
    "alpaca-py>=0.33",
    "yfinance>=0.2.50",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "python-dotenv>=1.0",
]

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6"]

[project.scripts]
allpath_trade = "allpath_trade.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
markers = ["integration: touches real network services; skipped unless credentials configured"]
addopts = "-m 'not integration'"

[tool.ruff]
line-length = 100
```

`.gitignore`:
```
__pycache__/
*.pyc
.venv/
.env
*.db
.pytest_cache/
.ruff_cache/
dist/
```

`.env.example`:
```
# Alpaca (paper account keys from https://app.alpaca.markets/paper/dashboard/overview)
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_PAPER=true

# LLM providers (used from Phase 3 on; set via Web UI later)
OPENROUTER_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
```

`allpath_trade/__init__.py`:
```python
__version__ = "0.1.0"
```

`tests/__init__.py`: empty file.

`tests/test_smoke.py`:
```python
import allpath_trade


def test_package_imports():
    assert allpath_trade.__version__
```

- [ ] **Step 2: Create env and run tests**

Run: `uv sync && uv run pytest -v`
Expected: `test_package_imports PASSED`

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: project scaffold (uv, pytest, ruff)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Settings + SettingsStore (runtime-writable .env)

**Files:**
- Create: `allpath_trade/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Settings(BaseSettings)` with fields `alpaca_api_key: str = ""`, `alpaca_secret_key: str = ""`, `alpaca_paper: bool = True`, `db_path: Path = Path("allpath_trade.db")`, `env_file: Path = Path(".env")` — loads from `.env` + env vars (env vars win).
  - `SettingsStore(env_file: Path)` with `.get(key: str) -> str | None`, `.set(key: str, value: str) -> None`, `.load() -> Settings`. `.set` persists to the `.env` file (create if missing), preserving other lines. This is the single write-path later used by both the Web UI settings page and the agent's `update_settings` tool (spec: LLM key must be set via Web UI first; broker creds may be set via UI or agent).

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
from pathlib import Path

from allpath_trade.config import Settings, SettingsStore


def test_settings_defaults(tmp_path: Path):
    s = Settings(_env_file=tmp_path / "nope.env")
    assert s.alpaca_paper is True
    assert s.alpaca_api_key == ""


def test_store_set_creates_and_updates_env_file(tmp_path: Path):
    env = tmp_path / ".env"
    store = SettingsStore(env)
    store.set("ALPACA_API_KEY", "k1")
    store.set("ALPACA_SECRET_KEY", "s1")
    store.set("ALPACA_API_KEY", "k2")  # update in place
    text = env.read_text()
    assert "ALPACA_API_KEY=k2" in text
    assert "ALPACA_SECRET_KEY=s1" in text
    assert text.count("ALPACA_API_KEY") == 1
    assert store.get("ALPACA_API_KEY") == "k2"


def test_store_load_returns_settings(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    env = tmp_path / ".env"
    store = SettingsStore(env)
    store.set("ALPACA_API_KEY", "abc")
    store.set("ALPACA_PAPER", "true")
    s = store.load()
    assert s.alpaca_api_key == "abc"
    assert s.alpaca_paper is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError` / `ImportError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/config.py`:
```python
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values, set_key
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    db_path: Path = Path("allpath_trade.db")


class SettingsStore:
    """Single read/write path for local config. Backed by a .env file.

    Real environment variables still override file values when loading
    Settings (pydantic-settings behavior)."""

    def __init__(self, env_file: Path = Path(".env")) -> None:
        self.env_file = env_file

    def get(self, key: str) -> str | None:
        if not self.env_file.exists():
            return None
        return dotenv_values(self.env_file).get(key)

    def set(self, key: str, value: str) -> None:
        self.env_file.touch(exist_ok=True)
        set_key(str(self.env_file), key, value, quote_mode="never")

    def load(self) -> Settings:
        return Settings(_env_file=self.env_file)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/config.py tests/test_config.py
git commit -m "feat: Settings + runtime-writable SettingsStore (.env-backed)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Broker domain models + abstract interface

**Files:**
- Create: `allpath_trade/broker/__init__.py`, `allpath_trade/broker/base.py`
- Test: `tests/test_broker_base.py`

**Interfaces:**
- Produces (used by every later task):
  - Enums: `OrderSide` (`BUY="buy"`, `SELL="sell"`), `OrderStatus` (`SUBMITTED`, `FILLED`, `PARTIALLY_FILLED`, `CANCELED`, `REJECTED` — lowercase string values).
  - Models: `Account(equity: Decimal, cash: Decimal, buying_power: Decimal)`; `Position(ticker: str, qty: Decimal, avg_entry_price: Decimal, market_value: Decimal, unrealized_pl: Decimal)`; `OrderIntent(ticker: str, side: OrderSide, qty: Decimal | None = None, notional: Decimal | None = None, reason: str, strategy_id: str | None = None)` — validator enforces exactly one of qty/notional, ticker uppercased; `Order(id: str, ticker: str, side: OrderSide, qty: Decimal | None, notional: Decimal | None, status: OrderStatus, filled_qty: Decimal, filled_avg_price: Decimal | None, submitted_at: datetime)`.
  - `Broker(ABC)` with attrs `name: str`, `is_paper: bool` and abstract methods `get_account() -> Account`, `get_positions() -> list[Position]`, `get_order(order_id: str) -> Order`, `get_orders(open_only: bool = True) -> list[Order]`, `submit_order(intent: OrderIntent) -> Order`, `cancel_order(order_id: str) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/test_broker_base.py`:
```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from allpath_trade.broker.base import Broker, OrderIntent, OrderSide


def test_intent_requires_exactly_one_of_qty_or_notional():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(
            ticker="AAPL", side=OrderSide.BUY, qty=Decimal("1"),
            notional=Decimal("100"), reason="x",
        )
    ok = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("100"), reason="x")
    assert ok.notional == Decimal("100")


def test_intent_uppercases_ticker_and_rejects_nonpositive():
    i = OrderIntent(ticker="aapl", side=OrderSide.SELL, qty=Decimal("2"), reason="x")
    assert i.ticker == "AAPL"
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal("0"), reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("-5"), reason="x")


def test_broker_is_abstract():
    with pytest.raises(TypeError):
        Broker()  # type: ignore[abstract]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_broker_base.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/broker/__init__.py`:
```python
from allpath_trade.broker.base import (
    Account,
    Broker,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)

__all__ = [
    "Account", "Broker", "Order", "OrderIntent", "OrderSide", "OrderStatus", "Position",
]
```

`allpath_trade/broker/base.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, field_validator, model_validator


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Account(BaseModel):
    equity: Decimal
    cash: Decimal
    buying_power: Decimal


class Position(BaseModel):
    ticker: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal


class OrderIntent(BaseModel):
    """A request to trade. The ONLY thing upper layers (LLM included) may
    produce; execution always goes through the risk gate."""

    ticker: str
    side: OrderSide
    qty: Decimal | None = None
    notional: Decimal | None = None  # dollar amount
    reason: str
    strategy_id: str | None = None

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _exactly_one_size(self) -> "OrderIntent":
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional is required")
        for val in (self.qty, self.notional):
            if val is not None and val <= 0:
                raise ValueError("order size must be positive")
        return self


class Order(BaseModel):
    id: str
    ticker: str
    side: OrderSide
    qty: Decimal | None
    notional: Decimal | None
    status: OrderStatus
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    submitted_at: datetime


class Broker(ABC):
    name: str = "abstract"
    is_paper: bool = True

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Order: ...

    @abstractmethod
    def get_orders(self, open_only: bool = True) -> list[Order]: ...

    @abstractmethod
    def submit_order(self, intent: OrderIntent) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> None: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_broker_base.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/broker tests/test_broker_base.py
git commit -m "feat: broker domain models and abstract Broker interface

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Alpaca adapter

**Files:**
- Create: `allpath_trade/broker/alpaca.py`
- Test: `tests/test_broker_alpaca.py`, `tests/test_broker_alpaca_integration.py`

**Interfaces:**
- Consumes: everything from `allpath_trade.broker.base` (Task 3), `Settings` (Task 2).
- Produces: `AlpacaBroker(api_key: str, secret_key: str, paper: bool = True, client: object | None = None)` implementing `Broker`. `client` param allows injecting a stub in tests. Market orders, `TimeInForce.DAY`.

- [ ] **Step 1: Write the failing unit test (stub client)**

`tests/test_broker_alpaca.py`:
```python
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from allpath_trade.broker.alpaca import AlpacaBroker
from allpath_trade.broker.base import OrderIntent, OrderSide, OrderStatus


def _raw_order(**over):
    base = dict(
        id="oid-1", symbol="AAPL", side=SimpleNamespace(value="buy"),
        qty="5", notional=None, status=SimpleNamespace(value="filled"),
        filled_qty="5", filled_avg_price="200.5",
        submitted_at=datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc),
    )
    base.update(over)
    return SimpleNamespace(**base)


class StubClient:
    def __init__(self):
        self.submitted = []

    def get_account(self):
        return SimpleNamespace(equity="10000", cash="4000", buying_power="8000")

    def get_all_positions(self):
        return [SimpleNamespace(symbol="AAPL", qty="5", avg_entry_price="190",
                                market_value="1002.5", unrealized_pl="52.5")]

    def submit_order(self, req):
        self.submitted.append(req)
        return _raw_order()

    def get_order_by_id(self, order_id):
        return _raw_order(id=order_id)

    def get_orders(self, filter=None):
        return [_raw_order()]

    def cancel_order_by_id(self, order_id):
        self.canceled = order_id


def make_broker():
    stub = StubClient()
    return AlpacaBroker("k", "s", paper=True, client=stub), stub


def test_get_account_maps_decimals():
    b, _ = make_broker()
    acct = b.get_account()
    assert acct.equity == Decimal("10000")
    assert acct.cash == Decimal("4000")


def test_get_positions_maps_fields():
    b, _ = make_broker()
    [p] = b.get_positions()
    assert p.ticker == "AAPL" and p.qty == Decimal("5")
    assert p.market_value == Decimal("1002.5")


def test_submit_qty_order():
    b, stub = make_broker()
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal("5"), reason="test")
    order = b.submit_order(intent)
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == Decimal("200.5")
    req = stub.submitted[0]
    assert req.symbol == "AAPL" and req.qty == 5.0 and req.notional is None


def test_submit_notional_order():
    b, stub = make_broker()
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("500"), reason="t")
    b.submit_order(intent)
    req = stub.submitted[0]
    assert req.notional == 500.0 and req.qty is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_broker_alpaca.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/broker/alpaca.py`:
```python
from __future__ import annotations

from decimal import Decimal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as _Side
from alpaca.trading.enums import QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from allpath_trade.broker.base import (
    Account,
    Broker,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)

# Alpaca order statuses collapsed onto our coarse OrderStatus
_STATUS_MAP = {
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 client: object | None = None) -> None:
        self.is_paper = paper
        self._client = client or TradingClient(api_key, secret_key, paper=paper)

    def get_account(self) -> Account:
        a = self._client.get_account()
        return Account(equity=Decimal(a.equity), cash=Decimal(a.cash),
                       buying_power=Decimal(a.buying_power))

    def get_positions(self) -> list[Position]:
        return [
            Position(ticker=p.symbol, qty=Decimal(p.qty),
                     avg_entry_price=Decimal(p.avg_entry_price),
                     market_value=Decimal(p.market_value),
                     unrealized_pl=Decimal(p.unrealized_pl))
            for p in self._client.get_all_positions()
        ]

    def get_order(self, order_id: str) -> Order:
        return self._to_order(self._client.get_order_by_id(order_id))

    def get_orders(self, open_only: bool = True) -> list[Order]:
        status = QueryOrderStatus.OPEN if open_only else QueryOrderStatus.ALL
        raw = self._client.get_orders(filter=GetOrdersRequest(status=status))
        return [self._to_order(o) for o in raw]

    def submit_order(self, intent: OrderIntent) -> Order:
        req = MarketOrderRequest(
            symbol=intent.ticker,
            side=_Side.BUY if intent.side == OrderSide.BUY else _Side.SELL,
            time_in_force=TimeInForce.DAY,
            qty=float(intent.qty) if intent.qty is not None else None,
            notional=float(intent.notional) if intent.notional is not None else None,
        )
        return self._to_order(self._client.submit_order(req))

    def cancel_order(self, order_id: str) -> None:
        self._client.cancel_order_by_id(order_id)

    @staticmethod
    def _to_order(o: object) -> Order:
        status = _STATUS_MAP.get(str(o.status.value), OrderStatus.SUBMITTED)
        return Order(
            id=str(o.id),
            ticker=o.symbol,
            side=OrderSide(o.side.value),
            qty=Decimal(o.qty) if o.qty is not None else None,
            notional=Decimal(o.notional) if o.notional is not None else None,
            status=status,
            filled_qty=Decimal(o.filled_qty or "0"),
            filled_avg_price=Decimal(o.filled_avg_price) if o.filled_avg_price else None,
            submitted_at=o.submitted_at,
        )
```

- [ ] **Step 4: Run unit tests**

Run: `uv run pytest tests/test_broker_alpaca.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Add integration test (opt-in)**

`tests/test_broker_alpaca_integration.py`:
```python
"""Real Alpaca paper-account round trip. Runs only with credentials:
    uv run pytest -m integration tests/test_broker_alpaca_integration.py -v
"""
import os

import pytest

from allpath_trade.broker.alpaca import AlpacaBroker

pytestmark = pytest.mark.integration

needs_keys = pytest.mark.skipif(
    not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")),
    reason="ALPACA_API_KEY / ALPACA_SECRET_KEY not set",
)


@needs_keys
def test_paper_account_roundtrip():
    b = AlpacaBroker(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    acct = b.get_account()
    assert acct.equity > 0
    assert isinstance(b.get_positions(), list)
    assert isinstance(b.get_orders(open_only=True), list)
```

Run: `uv run pytest tests/test_broker_alpaca_integration.py -v` → expected: `no tests ran` / deselected (integration excluded by default). If the executor has keys in env, optionally run `uv run pytest -m integration -v` and expect PASS/skip accordingly.

- [ ] **Step 6: Commit**

```bash
git add allpath_trade/broker/alpaca.py tests/test_broker_alpaca.py tests/test_broker_alpaca_integration.py
git commit -m "feat: Alpaca broker adapter (paper-first, market orders)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Data layer (models + yfinance source)

**Files:**
- Create: `allpath_trade/data/__init__.py`, `allpath_trade/data/base.py`, `allpath_trade/data/yf.py`
- Test: `tests/test_data.py`

**Interfaces:**
- Produces:
  - `Quote(ticker: str, price: Decimal, as_of: datetime)`; `Bar(ts: datetime, open: float, high: float, low: float, close: float, volume: int)`.
  - `DataSource(ABC)`: `get_quote(ticker: str) -> Quote`, `get_bars(ticker: str, days: int = 365) -> list[Bar]` (daily bars).
  - `YFinanceSource(ticker_factory=yfinance.Ticker)` implementing `DataSource`; `ticker_factory` injectable for tests.

- [ ] **Step 1: Write the failing test**

`tests/test_data.py`:
```python
from datetime import datetime
from decimal import Decimal

import pandas as pd

from allpath_trade.data.yf import YFinanceSource


class StubTicker:
    fast_info = {"last_price": 201.37}

    def history(self, period, interval="1d"):
        idx = pd.to_datetime(["2026-07-28", "2026-07-29"])
        return pd.DataFrame(
            {"Open": [199.0, 200.0], "High": [202.0, 203.0], "Low": [198.0, 199.5],
             "Close": [201.0, 202.5], "Volume": [1000, 1100]},
            index=idx,
        )


def make_source():
    return YFinanceSource(ticker_factory=lambda t: StubTicker())


def test_get_quote_returns_decimal_price():
    q = make_source().get_quote("aapl")
    assert q.ticker == "AAPL"
    assert q.price == Decimal("201.37")
    assert isinstance(q.as_of, datetime)


def test_get_bars_maps_dataframe():
    bars = make_source().get_bars("AAPL", days=2)
    assert len(bars) == 2
    assert bars[-1].close == 202.5
    assert bars[0].volume == 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_data.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/data/__init__.py`:
```python
from allpath_trade.data.base import Bar, DataSource, Quote
from allpath_trade.data.yf import YFinanceSource

__all__ = ["Bar", "DataSource", "Quote", "YFinanceSource"]
```

`allpath_trade/data/base.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class Quote(BaseModel):
    ticker: str
    price: Decimal
    as_of: datetime


class Bar(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class DataSource(ABC):
    @abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abstractmethod
    def get_bars(self, ticker: str, days: int = 365) -> list[Bar]: ...
```

`allpath_trade/data/yf.py`:
```python
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

import yfinance

from allpath_trade.data.base import Bar, DataSource, Quote


class YFinanceSource(DataSource):
    def __init__(self, ticker_factory: Callable[[str], object] = yfinance.Ticker) -> None:
        self._ticker = ticker_factory

    def get_quote(self, ticker: str) -> Quote:
        ticker = ticker.strip().upper()
        price = self._ticker(ticker).fast_info["last_price"]
        return Quote(ticker=ticker, price=Decimal(str(price)),
                     as_of=datetime.now(timezone.utc))

    def get_bars(self, ticker: str, days: int = 365) -> list[Bar]:
        ticker = ticker.strip().upper()
        df = self._ticker(ticker).history(period=f"{days}d", interval="1d")
        return [
            Bar(ts=ts.to_pydatetime(), open=float(r["Open"]), high=float(r["High"]),
                low=float(r["Low"]), close=float(r["Close"]), volume=int(r["Volume"]))
            for ts, r in df.iterrows()
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_data.py -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/data tests/test_data.py
git commit -m "feat: data layer with yfinance source

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Risk gate

**Files:**
- Create: `allpath_trade/risk/__init__.py`, `allpath_trade/risk/gate.py`
- Test: `tests/test_risk_gate.py`

**Interfaces:**
- Consumes: `OrderIntent`, `OrderSide`, `Account`, `Position` (Task 3).
- Produces:
  - `RiskLimits(BaseModel)`: `max_order_value: Decimal = Decimal("5000")`, `max_position_weight: Decimal = Decimal("0.25")`, `max_daily_trades: int = 10`, `min_cash_reserve: Decimal = Decimal("0")`, `allow_live: bool = False`.
  - `RiskDecision(BaseModel)`: `approved: bool`, `reasons: list[str] = []` (each reason a human-readable rejection cause; empty when approved).
  - `RiskGate(limits: RiskLimits)` with `check(intent: OrderIntent, *, account: Account, positions: list[Position], trades_today: int, is_paper: bool, price: Decimal) -> RiskDecision`. Pure/deterministic; collects ALL violated rules, not just the first.

- [ ] **Step 1: Write the failing test**

`tests/test_risk_gate.py`:
```python
from decimal import Decimal

import pytest

from allpath_trade.broker.base import Account, OrderIntent, OrderSide, Position
from allpath_trade.risk.gate import RiskGate, RiskLimits

ACCT = Account(equity=Decimal("10000"), cash=Decimal("5000"), buying_power=Decimal("10000"))
AAPL_POS = Position(ticker="AAPL", qty=Decimal("10"), avg_entry_price=Decimal("190"),
                    market_value=Decimal("2000"), unrealized_pl=Decimal("100"))


def buy(notional="1000", ticker="AAPL"):
    return OrderIntent(ticker=ticker, side=OrderSide.BUY,
                       notional=Decimal(notional), reason="t")


def sell(qty="5", ticker="AAPL"):
    return OrderIntent(ticker=ticker, side=OrderSide.SELL, qty=Decimal(qty), reason="t")


def check(intent, limits=None, positions=None, trades_today=0, is_paper=True,
          price=Decimal("200"), account=ACCT):
    gate = RiskGate(limits or RiskLimits())
    return gate.check(intent, account=account, positions=positions or [AAPL_POS],
                      trades_today=trades_today, is_paper=is_paper, price=price)


def test_approves_reasonable_buy():
    d = check(buy("1000"))
    assert d.approved and d.reasons == []


def test_rejects_live_when_not_allowed():
    d = check(buy(), is_paper=False)
    assert not d.approved
    assert any("live" in r.lower() for r in d.reasons)


def test_allows_live_when_enabled():
    d = check(buy(), limits=RiskLimits(allow_live=True), is_paper=False)
    assert d.approved


def test_rejects_order_value_above_cap():
    d = check(buy("6000"))
    assert not d.approved
    assert any("order value" in r.lower() for r in d.reasons)


def test_qty_order_value_uses_price():
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal("30"), reason="t")
    d = check(intent, price=Decimal("200"))  # 30*200 = 6000 > 5000
    assert not d.approved


def test_rejects_daily_trade_limit():
    d = check(buy(), trades_today=10)
    assert not d.approved
    assert any("daily trade" in r.lower() for r in d.reasons)


def test_rejects_position_weight_breach():
    # AAPL 2000/10000 = 20%; buying 1000 more -> 30% > 25% cap
    d = check(buy("1000"))
    assert not d.approved
    assert any("position weight" in r.lower() for r in d.reasons)


def test_approves_buy_within_weight():
    d = check(buy("400"))  # -> 24%
    assert d.approved


def test_rejects_buy_breaking_cash_reserve():
    limits = RiskLimits(min_cash_reserve=Decimal("4500"))
    d = check(buy("1000", ticker="MSFT"), limits=limits, positions=[])
    assert not d.approved
    assert any("cash reserve" in r.lower() for r in d.reasons)


def test_rejects_sell_exceeding_position():
    d = check(sell("11"))
    assert not d.approved
    assert any("exceeds position" in r.lower() for r in d.reasons)


def test_rejects_sell_of_unowned_ticker():
    d = check(sell("1", ticker="TSLA"))
    assert not d.approved


def test_collects_multiple_reasons():
    d = check(buy("6000"), trades_today=99)
    assert len(d.reasons) >= 2
```

(The two code blocks above together form the single file `tests/test_risk_gate.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_risk_gate.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/risk/__init__.py`:
```python
from allpath_trade.risk.gate import RiskDecision, RiskGate, RiskLimits

__all__ = ["RiskDecision", "RiskGate", "RiskLimits"]
```

`allpath_trade/risk/gate.py`:
```python
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from allpath_trade.broker.base import Account, OrderIntent, OrderSide, Position


class RiskLimits(BaseModel):
    max_order_value: Decimal = Decimal("5000")
    max_position_weight: Decimal = Decimal("0.25")  # fraction of equity
    max_daily_trades: int = 10
    min_cash_reserve: Decimal = Decimal("0")
    allow_live: bool = False


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = []


class RiskGate:
    """Deterministic pre-trade checks. Every order intent passes through here;
    there is no code path from the LLM to a broker that skips this gate."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def check(self, intent: OrderIntent, *, account: Account,
              positions: list[Position], trades_today: int,
              is_paper: bool, price: Decimal) -> RiskDecision:
        reasons: list[str] = []
        lim = self.limits
        order_value = intent.notional if intent.notional is not None else intent.qty * price
        pos = next((p for p in positions if p.ticker == intent.ticker), None)

        if not is_paper and not lim.allow_live:
            reasons.append("live trading is disabled (allow_live=false)")

        if order_value > lim.max_order_value:
            reasons.append(
                f"order value {order_value} exceeds max_order_value {lim.max_order_value}")

        if trades_today >= lim.max_daily_trades:
            reasons.append(
                f"daily trade limit reached ({trades_today}/{lim.max_daily_trades})")

        if intent.side == OrderSide.BUY:
            current = pos.market_value if pos else Decimal("0")
            if account.equity > 0:
                weight = (current + order_value) / account.equity
                if weight > lim.max_position_weight:
                    reasons.append(
                        f"resulting position weight {weight:.2%} exceeds "
                        f"max_position_weight {lim.max_position_weight:.0%}")
            if account.cash - order_value < lim.min_cash_reserve:
                reasons.append(
                    f"buy would drop cash below min_cash_reserve {lim.min_cash_reserve}")
        else:  # SELL — no shorting in v1
            held_qty = pos.qty if pos else Decimal("0")
            held_value = pos.market_value if pos else Decimal("0")
            if intent.qty is not None and intent.qty > held_qty:
                reasons.append(
                    f"sell qty {intent.qty} exceeds position ({held_qty} held)")
            if intent.notional is not None and intent.notional > held_value:
                reasons.append(
                    f"sell notional {intent.notional} exceeds position value {held_value}")

        return RiskDecision(approved=not reasons, reasons=reasons)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_risk_gate.py -v`
Expected: all PASSED (12 tests)

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/risk tests/test_risk_gate.py
git commit -m "feat: deterministic risk gate (paper-first, position/order/cash/trade caps)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: SQLite store + trade journal

**Files:**
- Create: `allpath_trade/store/__init__.py`, `allpath_trade/store/db.py`, `allpath_trade/store/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: `OrderIntent`, `Order` (Task 3), `RiskDecision` (Task 6).
- Produces:
  - `connect(path: Path | str) -> sqlite3.Connection` — row_factory=Row, executes idempotent schema (`CREATE TABLE IF NOT EXISTS trades ...`).
  - `TradeJournal(conn)` with `record(intent: OrderIntent, decision: RiskDecision, order: Order | None) -> int` (returns row id; status is `rejected` when decision not approved else the order's status), `trades_today(now: datetime | None = None) -> int` (counts non-rejected rows with UTC date == today), `recent(limit: int = 50) -> list[sqlite3.Row]` (newest first).

- [ ] **Step 1: Write the failing test**

`tests/test_journal.py`:
```python
from datetime import datetime, timezone
from decimal import Decimal

from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal


def make_journal(tmp_path):
    return TradeJournal(connect(tmp_path / "t.db"))


INTENT = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("500"),
                     reason="dip buy", strategy_id="aapl-long")
ORDER = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
              notional=Decimal("500"), status=OrderStatus.FILLED,
              filled_qty=Decimal("2.5"), filled_avg_price=Decimal("200"),
              submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc))


def test_record_submitted_and_recent(tmp_path):
    j = make_journal(tmp_path)
    rid = j.record(INTENT, RiskDecision(approved=True), ORDER)
    assert rid == 1
    [row] = j.recent()
    assert row["ticker"] == "AAPL"
    assert row["status"] == "filled"
    assert row["broker_order_id"] == "o1"
    assert row["strategy_id"] == "aapl-long"


def test_record_rejected(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=False, reasons=["too big"]), None)
    [row] = j.recent()
    assert row["status"] == "rejected"
    assert "too big" in row["risk_reasons"]


def test_trades_today_counts_only_executed_today(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    j.record(INTENT, RiskDecision(approved=False, reasons=["x"]), None)  # rejected: not counted
    assert j.trades_today() == 1


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    connect(path)
    connect(path)  # second connect must not fail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_journal.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/store/__init__.py`:
```python
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal

__all__ = ["TradeJournal", "connect"]
```

`allpath_trade/store/db.py`:
```python
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                -- UTC ISO-8601
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT,
    notional TEXT,
    status TEXT NOT NULL,            -- rejected | submitted | filled | ...
    reason TEXT NOT NULL,            -- human-readable intent reason
    strategy_id TEXT,
    risk_reasons TEXT NOT NULL DEFAULT '[]',  -- JSON list
    broker_order_id TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
```

`allpath_trade/store/journal.py`:
```python
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from allpath_trade.broker.base import Order, OrderIntent
from allpath_trade.risk.gate import RiskDecision


class TradeJournal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, intent: OrderIntent, decision: RiskDecision,
               order: Order | None) -> int:
        status = order.status.value if (decision.approved and order) else "rejected"
        cur = self._conn.execute(
            "INSERT INTO trades (ts, ticker, side, qty, notional, status, reason,"
            " strategy_id, risk_reasons, broker_order_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
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
        now = now or datetime.now(timezone.utc)
        day = now.date().isoformat()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE ts LIKE ? AND status != 'rejected'",
            (f"{day}%",),
        ).fetchone()
        return int(row["n"])

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_journal.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/store tests/test_journal.py
git commit -m "feat: SQLite store and trade journal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Order executor (wire gate + broker + journal)

**Files:**
- Create: `allpath_trade/execution.py`
- Test: `tests/test_execution.py`

**Interfaces:**
- Consumes: `Broker`, `OrderIntent`, `Order` (Task 3); `RiskGate`, `RiskDecision` (Task 6); `TradeJournal` (Task 7); `DataSource` (Task 5).
- Produces:
  - `ExecutionResult(BaseModel)`: `submitted: bool`, `order: Order | None`, `decision: RiskDecision`.
  - `Executor(broker: Broker, gate: RiskGate, journal: TradeJournal, data: DataSource)` with `execute(intent: OrderIntent) -> ExecutionResult`. Flow: quote → gate.check → (reject: journal + return) | (approve: broker.submit_order → journal → return). Broker exceptions are caught, journaled as status `rejected` with the error in risk_reasons, and re-raised as `ExecutionError`.
  - `ExecutionError(Exception)`.
  - This is THE only function later phases call to trade.

- [ ] **Step 1: Write the failing test**

`tests/test_execution.py`:
```python
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from allpath_trade.broker.base import (
    Account, Broker, Order, OrderIntent, OrderSide, OrderStatus, Position,
)
from allpath_trade.data.base import Bar, DataSource, Quote
from allpath_trade.execution import ExecutionError, Executor
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal


class FakeData(DataSource):
    def get_quote(self, ticker):
        return Quote(ticker=ticker, price=Decimal("200"),
                     as_of=datetime.now(timezone.utc))

    def get_bars(self, ticker, days=365):
        return []


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def __init__(self, fail=False):
        self.fail = fail
        self.submitted = []

    def get_account(self):
        return Account(equity=Decimal("10000"), cash=Decimal("8000"),
                       buying_power=Decimal("10000"))

    def get_positions(self):
        return [Position(ticker="AAPL", qty=Decimal("10"),
                         avg_entry_price=Decimal("190"),
                         market_value=Decimal("2000"),
                         unrealized_pl=Decimal("100"))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        if self.fail:
            raise RuntimeError("alpaca 500")
        self.submitted.append(intent)
        return Order(id="o1", ticker=intent.ticker, side=intent.side,
                     qty=intent.qty, notional=intent.notional,
                     status=OrderStatus.SUBMITTED, filled_qty=Decimal("0"),
                     filled_avg_price=None,
                     submitted_at=datetime.now(timezone.utc))

    def cancel_order(self, order_id):
        pass


def make_executor(tmp_path, fail=False, limits=None):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    broker = FakeBroker(fail=fail)
    ex = Executor(broker, RiskGate(limits or RiskLimits()), journal, FakeData())
    return ex, broker, journal


def buy(notional="500"):
    return OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                       notional=Decimal(notional), reason="t")


def test_approved_intent_is_submitted_and_journaled(tmp_path):
    ex, broker, journal = make_executor(tmp_path)
    res = ex.execute(buy())
    assert res.submitted and res.order.id == "o1"
    assert len(broker.submitted) == 1
    [row] = journal.recent()
    assert row["status"] == "submitted"


def test_rejected_intent_never_reaches_broker(tmp_path):
    ex, broker, journal = make_executor(tmp_path)
    res = ex.execute(buy("6000"))  # over max_order_value
    assert not res.submitted and res.order is None
    assert broker.submitted == []
    [row] = journal.recent()
    assert row["status"] == "rejected"


def test_broker_failure_is_journaled_and_raised(tmp_path):
    ex, broker, journal = make_executor(tmp_path, fail=True)
    with pytest.raises(ExecutionError):
        ex.execute(buy())
    [row] = journal.recent()
    assert row["status"] == "rejected"
    assert "alpaca 500" in row["risk_reasons"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_execution.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/execution.py`:
```python
from __future__ import annotations

from pydantic import BaseModel

from allpath_trade.broker.base import Broker, Order, OrderIntent
from allpath_trade.data.base import DataSource
from allpath_trade.risk.gate import RiskDecision, RiskGate
from allpath_trade.store.journal import TradeJournal


class ExecutionError(Exception):
    pass


class ExecutionResult(BaseModel):
    submitted: bool
    order: Order | None
    decision: RiskDecision


class Executor:
    """The single entry point for trading. Everything above (scheduler,
    agent tools) creates OrderIntents and calls execute()."""

    def __init__(self, broker: Broker, gate: RiskGate,
                 journal: TradeJournal, data: DataSource) -> None:
        self.broker = broker
        self.gate = gate
        self.journal = journal
        self.data = data

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        quote = self.data.get_quote(intent.ticker)
        decision = self.gate.check(
            intent,
            account=self.broker.get_account(),
            positions=self.broker.get_positions(),
            trades_today=self.journal.trades_today(),
            is_paper=self.broker.is_paper,
            price=quote.price,
        )
        if not decision.approved:
            self.journal.record(intent, decision, None)
            return ExecutionResult(submitted=False, order=None, decision=decision)

        try:
            order = self.broker.submit_order(intent)
        except Exception as exc:
            failed = RiskDecision(approved=False,
                                  reasons=[f"broker error: {exc}"])
            self.journal.record(intent, failed, None)
            raise ExecutionError(str(exc)) from exc

        self.journal.record(intent, decision, order)
        return ExecutionResult(submitted=True, order=order, decision=decision)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_execution.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests pass, 0 failures

- [ ] **Step 6: Commit**

```bash
git add allpath_trade/execution.py tests/test_execution.py
git commit -m "feat: order executor wiring risk gate, broker, journal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: CLI (`allpath-trade status`) + README quickstart

**Files:**
- Create: `allpath_trade/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `SettingsStore` (Task 2), `AlpacaBroker` (Task 4), `TradeJournal`/`connect` (Task 7).
- Produces: console script `allpath_trade` with subcommand `status` — prints account equity/cash, positions table, and last 5 journal entries. `main(argv: list[str] | None = None, broker_factory=None) -> int` (factory injectable for tests; returns exit code, 2 when credentials missing).

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
from datetime import datetime, timezone
from decimal import Decimal

from allpath_trade.broker.base import Account, Broker, Position
from allpath_trade.cli import main


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def get_account(self):
        return Account(equity=Decimal("10000"), cash=Decimal("4000"),
                       buying_power=Decimal("8000"))

    def get_positions(self):
        return [Position(ticker="AAPL", qty=Decimal("5"),
                         avg_entry_price=Decimal("190"),
                         market_value=Decimal("1000"),
                         unrealized_pl=Decimal("50"))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


def test_status_prints_account_and_positions(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # so allpath_trade.db lands in tmp
    code = main(["status"], broker_factory=lambda settings: FakeBroker())
    out = capsys.readouterr().out
    assert code == 0
    assert "10000" in out and "AAPL" in out and "paper" in out.lower()


def test_status_without_keys_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = main(["status"])  # no factory: builds from (empty) settings
    assert code == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

`allpath_trade/cli.py`:
```python
from __future__ import annotations

import argparse
import sys
from typing import Callable

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings, SettingsStore
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal


def _default_broker(settings: Settings) -> Broker:
    from allpath_trade.broker.alpaca import AlpacaBroker

    return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                        paper=settings.alpaca_paper)


def cmd_status(settings: Settings, broker: Broker) -> int:
    acct = broker.get_account()
    mode = "PAPER" if broker.is_paper else "LIVE"
    print(f"[{broker.name} / {mode.lower()}]")
    print(f"equity: {acct.equity}  cash: {acct.cash}  buying_power: {acct.buying_power}")
    positions = broker.get_positions()
    if positions:
        print("\npositions:")
        for p in positions:
            print(f"  {p.ticker:6} qty={p.qty} avg={p.avg_entry_price} "
                  f"value={p.market_value} pl={p.unrealized_pl}")
    else:
        print("\nno open positions")

    journal = TradeJournal(connect(settings.db_path))
    rows = journal.recent(limit=5)
    if rows:
        print("\nrecent trades:")
        for r in rows:
            print(f"  #{r['id']} {r['ts'][:19]} {r['side']} {r['ticker']} "
                  f"[{r['status']}] {r['reason']}")
    return 0


def main(argv: list[str] | None = None,
         broker_factory: Callable[[Settings], Broker] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="allpath_trade")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show account, positions, recent trades")
    args = parser.parse_args(argv)

    settings = SettingsStore().load()
    if args.command == "status":
        if broker_factory is None and not (
                settings.alpaca_api_key and settings.alpaca_secret_key):
            print("Missing credentials: set ALPACA_API_KEY / ALPACA_SECRET_KEY "
                  "in .env (see .env.example)", file=sys.stderr)
            return 2
        broker = (broker_factory or _default_broker)(settings)
        return cmd_status(settings, broker)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

`README.md`:
```markdown
# All Path Trading Agent

An open-source, self-hosted, LLM-powered **mid/long-term** trading agent
framework. It discusses your goals with you, co-creates strategies with
explicit take-profit/stop-loss rules, monitors daily, executes through your
own brokerage account under tiered authorization, and learns with you over
time. Package name: `allpath_trade`.

> Status: Phase 1 (execution foundation). Paper trading only by default.

## Quickstart

1. Install [uv](https://docs.astral.sh/uv/), then:

   ```bash
   uv sync
   ```

2. Copy `.env.example` to `.env` and fill in your
   [Alpaca paper account](https://app.alpaca.markets/paper/dashboard/overview) keys.

3. Verify the connection:

   ```bash
   uv run allpath-trade status
   ```

## Safety model

- **Paper-first**: live trading is off unless you explicitly enable it.
- Every order passes a **deterministic risk gate** (order value cap, position
  weight cap, daily trade cap, cash reserve) that the LLM cannot bypass.
- Credentials stay in your local `.env`; nothing is ever uploaded.
- All trades and rejections are journaled in a local SQLite DB.

## Development

```bash
uv run pytest        # unit tests
uv run pytest -m integration   # needs Alpaca paper keys in env
uv run ruff check .
```
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 2 PASSED

- [ ] **Step 5: Full suite + lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: all pass, no lint errors

- [ ] **Step 6: Commit**

```bash
git add allpath_trade/cli.py README.md tests/test_cli.py
git commit -m "feat: allpath-trade status CLI and README quickstart

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Phase 1 Definition of Done

- `uv run pytest` green; `uv run ruff check .` clean.
- With real Alpaca paper keys in `.env`: `uv run allpath-trade status` prints the
  paper account, and `uv run pytest -m integration` passes.
- No code path submits an order without passing `RiskGate.check` (only
  `Executor.execute` calls `broker.submit_order` outside tests).

## Later phases (separate plans)

2. Strategy engine (YAML strategy docs, restricted-expression rule evaluator,
   versioning) + sentinel loop (APScheduler, every 2h during market hours,
   configurable) — hard rules auto-execute via `Executor`, soft rules enqueue
   agent review.
3. Agent core: LLM provider abstraction (Anthropic native + OpenAI-compatible
   for OpenAI/OpenRouter; OpenRouter default for testing), tool loop
   (`get_quote`, `get_bars`, `web_search`, `get_portfolio`, `propose_order`,
   `read/update_strategy`, `read/write_memory`, `update_settings`), context
   assembly.
4. Memory system: four layers (user profile, strategy memory, stock dossiers,
   lessons) + cross-cutting consolidation step after every loop.
5. Web UI (chat + dashboard + pending-confirmation queue + settings page for
   LLM/broker keys) + SMTP notifications.
6. Reflection loops: daily deep review (after market close), post-trade
   retrospectives.
