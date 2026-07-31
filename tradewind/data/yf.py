from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import yfinance

from tradewind.data.base import Bar, DataSource, Quote


class YFinanceSource(DataSource):
    def __init__(self, ticker_factory: Callable[[str], object] = yfinance.Ticker) -> None:
        self._ticker = ticker_factory

    def get_quote(self, ticker: str) -> Quote:
        ticker = ticker.strip().upper()
        price = self._ticker(ticker).fast_info["last_price"]
        return Quote(ticker=ticker, price=Decimal(str(price)),
                     as_of=datetime.now(UTC))

    def get_bars(self, ticker: str, days: int = 365) -> list[Bar]:
        ticker = ticker.strip().upper()
        df = self._ticker(ticker).history(period=f"{days}d", interval="1d")
        return [
            Bar(ts=ts.to_pydatetime(), open=float(r["Open"]), high=float(r["High"]),
                low=float(r["Low"]), close=float(r["Close"]), volume=int(r["Volume"]))
            for ts, r in df.iterrows()
        ]
