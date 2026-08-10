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
        # cached-per-Ticker-instance object. `last_price` below populates it
        # by fetching a 1y daily-bar history (FastInfo._get_1y_prices).
        # `regular_market_previous_close` is read deliberately instead of
        # the more obvious `previous_close`: `previous_close` pulls from a
        # *different* cache slot (a separate 5d/1h/prepost history call --
        # a second Yahoo request, sometimes a third if that comes back
        # empty and it falls back to `.info`), while
        # `regular_market_previous_close` reuses the same 1y DataFrame
        # `last_price` already populated on this FastInfo instance, so it
        # costs no extra network round trip.
        fast_info = self._ticker(ticker).fast_info
        try:
            price = fast_info["last_price"]
        except KeyError:
            price = None
        if price is None:
            raise ValueError(f"no price available for {ticker}")
        try:
            previous_close = fast_info["regular_market_previous_close"]
        except Exception:  # noqa: BLE001 — real yfinance raises rate-limit/HTTP
            # errors (e.g. YFRateLimitError) out of the history() call this
            # triggers on a cache miss, not just KeyError; losing the day's
            # comparison point must not cost the price itself, which is
            # already resolved above.
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
