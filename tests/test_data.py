from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import pandas as pd
import pytest

from allpath_trade.data.yf import YFinanceSource


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
