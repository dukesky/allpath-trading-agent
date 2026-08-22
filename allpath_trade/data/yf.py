from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import yfinance

from allpath_trade.data.base import Bar, DataSource, Quote

# shadow-dual-active T4 review (Important 3): quote-fetch amplification on
# the sentinel hot path. `ShadowLedger.get_account`/`get_positions` call
# `get_quote` once per position on every dashboard render and every
# sentinel tick, on top of paper's own strategy-quote calls sharing this
# same process -- measured at 14 fetches per dual tick versus 2 before
# dual-active. `fast_info` is yfinance's own lazily-populated cache, but
# it's scoped PER TICKER INSTANCE, and `self._ticker(ticker)` below builds
# a fresh one on every call -- so it never actually saves a request. This
# cache is deliberately MODULE-level, not per-`YFinanceSource`-instance:
# every account's data source is its own instance (app.py's per-account
# bundles), so a per-instance cache would do nothing to stop one account's
# calls from amplifying the other's rate-limit exposure. One shared cache
# here is the one place a fix benefits every caller in the process.
#
# Keyed by ticker; value is (fetched_at, Quote) on success or
# (fetched_at, Exception) on failure -- a failure is cached too, so a
# sustained rate-limit doesn't turn into a fresh 1y-history download from
# every caller for the rest of the TTL window. Every caller of get_quote
# already treats a raised exception as "no fresh price right now" (see
# ShadowLedger._valuation_price, web/routes/dashboard.py's _cached_quote,
# Reflector._positions_with_change) -- re-raising the cached exception on a
# hit costs them nothing they weren't already handling. 60s TTL: short
# enough that a stale valuation never persists long, long enough to
# collapse the N-positions-same-ticker and repeated-within-one-tick cases
# that actually drive the amplification. Guarded by a lock since the
# sentinel and a concurrent dashboard/chat request can call this from
# different threads at once.
_QUOTE_CACHE_TTL_SECONDS = 60
_quote_cache: dict[str, tuple[float, Quote | Exception]] = {}
_quote_cache_lock = threading.Lock()


class YFinanceSource(DataSource):
    def __init__(self, ticker_factory: Callable[[str], object] = yfinance.Ticker) -> None:
        self._ticker = ticker_factory

    def get_quote(self, ticker: str) -> Quote:
        ticker = ticker.strip().upper()
        with _quote_cache_lock:
            cached = _quote_cache.get(ticker)
            if cached is not None and time.monotonic() - cached[0] < _QUOTE_CACHE_TTL_SECONDS:
                result = cached[1]
                if isinstance(result, Exception):
                    raise result
                return result
        try:
            quote = self._fetch_quote(ticker)
        except Exception as exc:
            # Cache-and-reraise: any exception the real network call can
            # throw (ValueError for no price, yfinance's own rate-limit/
            # HTTP errors, ...) must be cached the same way a success is,
            # or a sustained failure would keep hitting the network every
            # call. A bare `except Exception` here needs no lint waiver --
            # this branch always re-raises immediately below, it never
            # swallows anything.
            with _quote_cache_lock:
                _quote_cache[ticker] = (time.monotonic(), exc)
            raise
        with _quote_cache_lock:
            _quote_cache[ticker] = (time.monotonic(), quote)
        return quote

    def _fetch_quote(self, ticker: str) -> Quote:
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
