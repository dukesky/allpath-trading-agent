from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class Quote(BaseModel):
    ticker: str
    price: Decimal
    as_of: datetime
    # Optional so every existing constructor call (tests, other DataSource
    # implementations) stays valid without an update; None means "no prior
    # close known" and callers must degrade honestly rather than fabricate a
    # direction (see summarize_strategy in web/routes/dashboard.py).
    previous_close: Decimal | None = None


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
