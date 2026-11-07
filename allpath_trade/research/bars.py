"""Daily bars for the counterfactual replay, cached on disk so a rerun with
the same cache gives the same numbers.

Source: yfinance, the repo's market-data dependency (`data/yf.py`). Bars are
RAW (`auto_adjust=False`): rule thresholds were written against the prices
the sentinel saw, not dividend/split-adjusted ones. `YFinanceSource.get_bars`
is not reused because it is period-based and adjusted.

Cache layout: `<dir>/<TICKER>.csv` (date,open,high,low,close) plus
`<dir>/<TICKER>.range` holding the requested `start end` the file covers.
"""
from __future__ import annotations

import csv
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class DailyBar:
    date: str
    open: float
    high: float
    low: float
    close: float


Fetch = Callable[[str, str, str], list[DailyBar]]


def yfinance_fetch(ticker: str, start: str, end: str) -> list[DailyBar]:
    import yfinance

    end_excl = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    df = yfinance.Ticker(ticker).history(start=start, end=end_excl, interval="1d",
                                         auto_adjust=False)
    return [DailyBar(date=ts.date().isoformat(), open=round(float(r["Open"]), 4),
                     high=round(float(r["High"]), 4), low=round(float(r["Low"]), 4),
                     close=round(float(r["Close"]), 4))
            for ts, r in df.iterrows()]


class BarCache:
    def __init__(self, directory: Path | str, fetch: Fetch | None = yfinance_fetch) -> None:
        self.directory = Path(directory)
        self._fetch = fetch

    def _paths(self, ticker: str) -> tuple[Path, Path]:
        safe = ticker.upper().replace("/", "_")
        return self.directory / f"{safe}.csv", self.directory / f"{safe}.range"

    def _read(self, ticker: str) -> tuple[tuple[str, str] | None, list[DailyBar]]:
        data, rng = self._paths(ticker)
        if not (data.exists() and rng.exists()):
            return None, []
        start, end = rng.read_text().split()
        with data.open() as f:
            bars = [DailyBar(date=r["date"], open=float(r["open"]), high=float(r["high"]),
                             low=float(r["low"]), close=float(r["close"]))
                    for r in csv.DictReader(f)]
        return (start, end), bars

    def get(self, ticker: str, start: str, end: str) -> list[DailyBar]:
        covered, bars = self._read(ticker)
        if covered is None or covered[0] > start or covered[1] < end:
            if self._fetch is None:
                raise LookupError(f"no cached bars for {ticker} covering {start}..{end} "
                                  "(run without --offline to fetch)")
            lo = min(start, covered[0]) if covered else start
            hi = max(end, covered[1]) if covered else end
            # yfinance can return a NaN field (seen: META's close on the
            # fetch day); such a bar is unusable, so it is never cached.
            bars = sorted((b for b in self._fetch(ticker, lo, hi)
                           if all(math.isfinite(x) for x in (b.open, b.high, b.low, b.close))),
                          key=lambda b: b.date)
            self.directory.mkdir(parents=True, exist_ok=True)
            data, rng = self._paths(ticker)
            with data.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close"])
                for b in bars:
                    w.writerow([b.date, b.open, b.high, b.low, b.close])
            rng.write_text(f"{lo} {hi}\n")
        return [b for b in bars if start <= b.date <= end]
