from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import Enum

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
