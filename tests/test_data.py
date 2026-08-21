import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import pandas as pd
import pytest

from allpath_trade.data import yf as yf_module
from allpath_trade.data.yf import YFinanceSource


@pytest.fixture(autouse=True)
def _clear_quote_cache():
    # shadow-dual-active T4 review (Important 3): `_quote_cache` is
    # module-level so it survives across CALLERS within the process, which
    # is the point -- but that means it also survives across *tests*
    # sharing the same ticker (AAPL) unless cleared, making assertions
    # depend on test order (same pattern as
    # tests/test_web_dashboard.py's `_clear_quote_cache`).
    yf_module._quote_cache.clear()
    yield
    yf_module._quote_cache.clear()


class YFRateLimitError(Exception):
    """Stand-in for yfinance's own exceptions.YFRateLimitError -- real
    fast_info["regular_market_previous_close"] access can raise this (or
    other request-layer exceptions) out of the history() call it triggers
    on a cache miss, never KeyError. A plain-dict fake wouldn't exercise
    that shape at all."""


class StubTicker:
    fast_info: ClassVar[dict] = {"last_price": 201.37, "regular_market_previous_close": 198.20}

    def history(self, period, interval="1d"):
        idx = pd.to_datetime(["2026-07-28", "2026-07-29"])
        return pd.DataFrame(
            {"Open": [199.0, 200.0], "High": [202.0, 203.0], "Low": [198.0, 199.5],
             "Close": [201.0, 202.5], "Volume": [1000, 1100]},
            index=idx,
        )


class NoPriceTicker:
    fast_info: ClassVar[dict] = {"last_price": None}

    def history(self, period, interval="1d"):
        return pd.DataFrame()


class PreviousCloseFailsTicker:
    """fast_info that has a price but raises fetching previous_close --
    mirrors a real FastInfo whose regular_market_previous_close access hits
    a rate limit or other transport error while last_price already
    resolved. The price must survive; only the day-change comparison is
    lost."""

    class _FastInfo:
        def __getitem__(self, key):
            if key == "last_price":
                return 201.37
            raise YFRateLimitError("rate limited")

    fast_info = _FastInfo()

    def history(self, period, interval="1d"):
        return pd.DataFrame()


def make_source():
    return YFinanceSource(ticker_factory=lambda t: StubTicker())


def test_get_quote_returns_decimal_price():
    q = make_source().get_quote("aapl")
    assert q.ticker == "AAPL"
    assert q.price == Decimal("201.37")
    assert q.previous_close == Decimal("198.20")
    assert isinstance(q.as_of, datetime)


def test_get_quote_previous_close_failure_leaves_price_intact():
    # A previous-close read that raises (rate limit, or any other
    # transport error -- real yfinance never raises plain KeyError here,
    # see PreviousCloseFailsTicker) must not cost the price: the price was
    # already resolved from a separate fast_info access above it.
    source = YFinanceSource(ticker_factory=lambda t: PreviousCloseFailsTicker())
    q = source.get_quote("aapl")
    assert q.price == Decimal("201.37")
    assert q.previous_close is None


def test_get_bars_maps_dataframe():
    bars = make_source().get_bars("AAPL", days=2)
    assert len(bars) == 2
    assert bars[-1].close == 202.5
    assert bars[0].volume == 1000


def test_get_quote_raises_when_no_price():
    source = YFinanceSource(ticker_factory=lambda t: NoPriceTicker())
    with pytest.raises(ValueError, match="no price available for AAPL"):
        source.get_quote("aapl")


# -- shadow-dual-active T4 review Important 3: quote-fetch amplification ----

class CountingTickerFactory:
    """Counts how many times a fresh Ticker was actually constructed --
    the real cost this cache exists to collapse (each construction is a
    fresh fast_info, i.e. a fresh 1y-history download)."""

    def __init__(self, ticker_cls=StubTicker):
        self.ticker_cls = ticker_cls
        self.calls: list[str] = []

    def __call__(self, ticker: str):
        self.calls.append(ticker)
        return self.ticker_cls()


def test_get_quote_caches_across_positions_sharing_one_ticker():
    # N positions on the same ticker (ShadowLedger.get_account/get_positions
    # valuing every position) must cost exactly one underlying fetch, not N.
    factory = CountingTickerFactory()
    source = YFinanceSource(ticker_factory=factory)
    for _ in range(5):
        q = source.get_quote("AAPL")
        assert q.price == Decimal("201.37")
    assert factory.calls == ["AAPL"]


def test_get_quote_cache_is_per_ticker_not_global():
    factory = CountingTickerFactory()
    source = YFinanceSource(ticker_factory=factory)
    source.get_quote("AAPL")
    source.get_quote("MSFT")
    source.get_quote("AAPL")
    assert factory.calls == ["AAPL", "MSFT"]


def test_get_quote_repeated_call_within_ttl_does_not_refetch(monkeypatch):
    factory = CountingTickerFactory()
    source = YFinanceSource(ticker_factory=factory)
    t = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: t[0])

    source.get_quote("AAPL")
    t[0] += yf_module._QUOTE_CACHE_TTL_SECONDS - 1
    source.get_quote("AAPL")
    assert factory.calls == ["AAPL"]


def test_get_quote_refetches_after_ttl_expires(monkeypatch):
    factory = CountingTickerFactory()
    source = YFinanceSource(ticker_factory=factory)
    t = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: t[0])

    source.get_quote("AAPL")
    t[0] += yf_module._QUOTE_CACHE_TTL_SECONDS + 1
    source.get_quote("AAPL")
    assert factory.calls == ["AAPL", "AAPL"]


def test_get_quote_failure_is_cached_too_not_retried_every_call():
    # A sustained rate-limit must not turn into a fresh network hit from
    # every single caller within the TTL window -- the whole point of
    # collapsing 14 fetches/tick down to one per distinct ticker.
    factory = CountingTickerFactory(ticker_cls=NoPriceTicker)
    source = YFinanceSource(ticker_factory=factory)
    for _ in range(3):
        with pytest.raises(ValueError, match="no price available for AAPL"):
            source.get_quote("AAPL")
    assert factory.calls == ["AAPL"]


def test_get_quote_cache_is_thread_safe_under_concurrent_callers():
    factory = CountingTickerFactory()
    source = YFinanceSource(ticker_factory=factory)
    errors = []

    def worker():
        try:
            source.get_quote("AAPL")
        except Exception as exc:  # noqa: BLE001 — surfaced via `errors` below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors
    # Some interleaving before the first write is acceptable (a handful of
    # concurrent misses racing to populate the cache), but the lock must
    # still bound it far below one fetch per thread.
    assert len(factory.calls) < len(threads)
