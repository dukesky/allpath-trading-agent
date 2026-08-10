from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import yfinance

from allpath_trade.data.base import Bar, DataSource, Quote


class YFinanceSource(DataSource):
    def __init__(self, ticker_factory: Callable[[str], object] = yfinance.Ticker) -> None:
        self._ticker = ticker_factory

    def get_quote(self, ticker: str) -> Quote:
        ticker = ticker.strip().upper()
        # A single fast_info access -- it's yfinance's own lazily-populated,
        # cached-per-Ticker-instance dict, so reading previous_close off the
        # same object below costs no extra network round trip.
        fast_info = self._ticker(ticker).fast_info
        try:
            price = fast_info["last_price"]
        except KeyError:
            price = None
        if price is None:
            raise ValueError(f"no price available for {ticker}")
        try:
            previous_close = fast_info["previous_close"]
        except KeyError:
            previous_close = None
        return Quote(
            ticker=ticker,
            price=Decimal(str(price)),
            previous_close=Decimal(str(previous_close)) if previous_close is not None else None,
            as_of=datetime.now(UTC),
        )

    def get_bars(self, ticker: str, days: int = 365) -> list[Bar]:
        ticker = ticker.strip().upper()
        df = self._ticker(ticker).history(period=f"{days}d", interval="1d")
        return [
            Bar(ts=ts.to_pydatetime(), open=float(r["Open"]), high=float(r["High"]),
                low=float(r["Low"]), close=float(r["Close"]), volume=int(r["Volume"]))
            for ts, r in df.iterrows()
        ]
