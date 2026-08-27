from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import NamedTuple

from pydantic import BaseModel, field_validator, model_validator

# Sane upper bound for any order size this app ever deals with -- a value
# like 1e400 IS finite per Decimal.is_finite() (pydantic's own
# `finite_number` check on the Decimal fields below already rejects
# Infinity/NaN, but lets a huge-but-finite magnitude straight through), and
# is exactly as bricking once it flows into equity/market_value arithmetic
# downstream. The single definition every numeric-input guard across the
# app shares -- shadow_tools.py's `_parse_money` imports this constant
# rather than keeping its own independent copy.
_MAX_MAGNITUDE = Decimal("1e12")


class BrokerError(Exception):
    """Base class for every failure that originates in the broker layer.

    Introduced with `BrokerNotConfigured` below rather than as a
    retrofit of the existing brokers: `AlpacaBroker` still lets the
    vendor SDK's own exceptions propagate (callers already treat any
    exception out of a broker call as "broker unavailable"), so this
    exists so that the ONE broker-layer condition callers must be able to
    single out -- "not configured yet", a setup state rather than an
    outage -- has somewhere to hang, without forcing a rewrite of the
    vendor error surface that nothing needs today.
    """


class BrokerNotConfigured(BrokerError):
    """The account has no usable credentials yet -- raised by every method
    of `broker.unconfigured.UnconfiguredBroker`.

    Distinct from every other broker failure because it is not a failure
    at all: it means the first-run setup wizard has not been completed.
    Callers that can say something better than "broker unavailable"
    (the sentinel, the scheduler, the dashboard heartbeat) catch this
    specifically and point the user at setup instead.
    """


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OccParts(NamedTuple):
    """Parsed components of an OCC option symbol."""
    root: str
    expiry: date
    right: str
    strike: Decimal


def parse_occ_symbol(ticker: str) -> OccParts | None:
    """Parse an OCC option symbol into its components.

    OCC pattern: root (1-6 letters) + YYMMDD + C/P + strike (8 digits).
    Returns None if the ticker does not match the OCC pattern or has an
    invalid date (e.g., month 99, day 32).

    Examples:
        parse_occ_symbol("META260918C00600000") ->
            OccParts(root="META", expiry=date(2026,9,18), right="call", strike=Decimal("600"))
        parse_occ_symbol("META260918P00123500") ->
            OccParts(root="META", expiry=date(2026,9,18), right="put", strike=Decimal("123.5"))
        parse_occ_symbol("META") -> None
        parse_occ_symbol("META269932C00600000") -> None (invalid month 99)
    """
    pattern = r'(?P<root>[A-Z]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})'
    match = re.fullmatch(pattern, ticker)
    if not match:
        return None

    root = match.group("root")
    ymd = match.group("ymd")
    cp = match.group("cp")
    strike_str = match.group("strike")

    # Parse YYmmdd into date (YY is 20YY)
    year = 2000 + int(ymd[:2])
    month = int(ymd[2:4])
    day = int(ymd[4:6])

    # Try to construct the date; return None if invalid (e.g., month 99, day 32)
    try:
        expiry = date(year, month, day)
    except ValueError:
        return None

    # Convert right: C -> "call", P -> "put"
    right = "call" if cp == "C" else "put"

    # Convert strike: 8 digits divided by 1000 to Decimal
    strike_int = int(strike_str)
    strike = Decimal(strike_int) / Decimal(1000)

    return OccParts(root=root, expiry=expiry, right=right, strike=strike)


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
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker must be non-empty")
        return v

    @model_validator(mode="after")
    def _exactly_one_size(self) -> OrderIntent:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional is required")
        for val in (self.qty, self.notional):
            if val is not None and val <= 0:
                raise ValueError("order size must be positive")
            if val is not None and val.copy_abs() > _MAX_MAGNITUDE:
                raise ValueError(f"order size {val} exceeds max magnitude {_MAX_MAGNITUDE}")
        return self


class OptionIntent(BaseModel):
    """A request to trade an option contract. Like OrderIntent, this is the
    ONLY thing upper layers (LLM included) may produce; execution always
    goes through the risk gate."""

    underlying: str            # e.g. "META" (upper, validated non-empty)
    right: str                 # "call" | "put"
    occ_symbol: str            # e.g. "META260918C00600000"
    side: OrderSide            # BUY (open) or SELL (close)
    qty: int                   # contracts, >= 1
    est_premium: Decimal       # total dollars (ask*100*qty); 0 for closes
    reason: str
    strategy_id: str | None = None

    @field_validator("underlying")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("underlying must be non-empty")
        return v

    @field_validator("right")
    @classmethod
    def _validate_right(cls, v: str) -> str:
        v_lower = v.lower()
        if v_lower not in ("call", "put"):
            raise ValueError("right must be 'call' or 'put'")
        return v_lower

    @field_validator("qty")
    @classmethod
    def _validate_qty(cls, v: int) -> int:
        if v < 1:
            raise ValueError("qty must be >= 1")
        return v

    @field_validator("est_premium")
    @classmethod
    def _validate_premium(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("est_premium must be >= 0")
        return v


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
    filled_at: datetime | None = None


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

    def get_equity_history(self, days: int) -> list[tuple[datetime, Decimal]]:
        """Equity curve for the last `days` days, oldest first, empty
        (equity == 0) days already filtered out. Deliberately NOT
        `@abstractmethod` -- unlike every other Broker method above, dozens
        of `FakeBroker` subclasses across the test suite construct today
        without implementing this, and this is a new addition (the
        dashboard equity chart); forcing a stub onto every one of them just
        to keep instantiating would be a lot of churn for a feature that
        already degrades cleanly ("No history yet") on an empty list.
        AlpacaBroker overrides this for real data."""
        return []
