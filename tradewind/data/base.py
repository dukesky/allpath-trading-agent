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
