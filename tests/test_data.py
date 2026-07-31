from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import pandas as pd

from tradewind.data.yf import YFinanceSource


class StubTicker:
    fast_info: ClassVar[dict] = {"last_price": 201.37}

    def history(self, period, interval="1d"):
        idx = pd.to_datetime(["2026-07-28", "2026-07-29"])
        return pd.DataFrame(
            {"Open": [199.0, 200.0], "High": [202.0, 203.0], "Low": [198.0, 199.5],
             "Close": [201.0, 202.5], "Volume": [1000, 1100]},
            index=idx,
        )


def make_source():
    return YFinanceSource(ticker_factory=lambda t: StubTicker())


def test_get_quote_returns_decimal_price():
    q = make_source().get_quote("aapl")
    assert q.ticker == "AAPL"
    assert q.price == Decimal("201.37")
    assert isinstance(q.as_of, datetime)


def test_get_bars_maps_dataframe():
    bars = make_source().get_bars("AAPL", days=2)
    assert len(bars) == 2
    assert bars[-1].close == 202.5
    assert bars[0].volume == 1000
