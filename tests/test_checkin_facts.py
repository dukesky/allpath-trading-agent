"""Check-in live facts: the authoritative account snapshot the question
generator reads (coordinator 2026-11-07: the first real check-in quoted cost
basis as the price, stale position sizes and a breaker that had been reset)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from allpath_trade.broker.base import Account, Position
from allpath_trade.checkin_facts import live_snapshot
from allpath_trade.data.base import Quote

NOW = datetime(2026, 11, 7, 1, 0, tzinfo=UTC)
D = Decimal


class FakeBroker:
    def __init__(self, positions=(), equity="261000", cash="43600", fail=False):
        self.positions = list(positions)
        self.equity, self.cash, self.fail = D(equity), D(cash), fail

    def get_account(self):
        if self.fail:
            raise RuntimeError("alpaca 503")
        return Account(equity=self.equity, cash=self.cash, buying_power=self.cash)

    def get_positions(self):
        return self.positions


class FakeData:
    def __init__(self, prices=None, fail=()):
        self.prices = {k: D(v) for k, v in (prices or {}).items()}
        self.fail = set(fail)

    def get_quote(self, ticker):
        if ticker in self.fail or ticker not in self.prices:
            raise RuntimeError(f"no quote for {ticker}")
        return Quote(ticker=ticker, price=self.prices[ticker], as_of=NOW)


class FakeBreaker:
    def __init__(self, tripped=None, peak=None, halt="0.15"):
        self._tripped, self._peak, self.halt_pct = tripped, peak, D(halt)

    def tripped_at(self):
        return self._tripped

    def peak(self):
        return D(self._peak) if self._peak else None


def pos(ticker, qty, avg, value, pl):
    return Position(ticker=ticker, qty=D(qty), avg_entry_price=D(avg),
                    market_value=D(value), unrealized_pl=D(pl))


CRDO = pos("CRDO", "57.67", "150.00", "6950.00", "-1700.50")
FIG = pos("FIG", "100", "35.87", "1881.00", "-1706.00")
FIG_CALLS = pos("FIG270115C00020000", "8", "4.10", "1600.00", "-1680.00")


def _snap(positions=(), prices=None, breaker=None, **kw):
    return live_snapshot(broker=FakeBroker(positions, **kw), data=FakeData(prices),
                         breaker=breaker, now=NOW)


def test_header_says_it_is_live_and_authoritative():
    text = _snap()
    assert "LIVE" in text.splitlines()[0]
    assert "2026-11-07 01:00 UTC" in text
    assert "equity $261,000.00" in text and "cash $43,600.00 (16.7% of equity)" in text


def test_stock_line_uses_live_price_and_labels_cost_basis():
    text = _snap([CRDO], prices={"CRDO": "120.51"})
    [line] = [ln for ln in text.splitlines() if ln.startswith("- CRDO")]
    assert "last $120.51" in line
    assert "value $6,950.00 (2.66% of equity)" in line
    assert "cost basis $150.00/sh" in line
    assert "unrealized -19.66%" in line


def test_stock_price_falls_back_to_broker_mark():
    text = _snap([FIG], prices={})
    [line] = [ln for ln in text.splitlines() if ln.startswith("- FIG:")]
    assert "last $18.81 (broker mark)" in line


def test_option_line_spells_out_contract_exposure_and_moneyness():
    text = _snap([FIG, FIG_CALLS], prices={"FIG": "18.81"})
    [line] = [ln for ln in text.splitlines() if ln.startswith("- FIG 2027-01-15")]
    assert "$20 call" in line
    assert "8 contracts (800 sh exposure)" in line
    assert "mark $2.00/sh" in line
    assert "cost basis $4.10/sh premium" in line
    assert "FIG last $18.81" in line
    assert "out of the money" in line


def test_in_the_money_put_and_unknown_underlying():
    put = pos("FIG270115P00020000", "1", "1.00", "150.00", "50.00")
    assert "in the money" in _snap([put], prices={"FIG": "18.81"})
    assert "underlying price unavailable" in _snap([put], prices={})


def test_no_positions():
    assert "no open positions" in _snap()


def test_breaker_not_tripped_after_reset():
    text = _snap(breaker=FakeBreaker(tripped=None, peak="300000"))
    [line] = [ln for ln in text.splitlines() if ln.startswith("Drawdown breaker")]
    assert "NOT tripped" in line and "13.0% below peak $300,000.00" in line
    assert "trips at 15.0%" in line


def test_breaker_tripped():
    text = _snap(breaker=FakeBreaker(tripped="2026-11-06T20:00:00+00:00",
                                     peak="313000"))
    [line] = [ln for ln in text.splitlines() if ln.startswith("Drawdown breaker")]
    assert "TRIPPED since 2026-11-06T20:00" in line and "16.6% below" in line


def test_breaker_absent_or_disabled():
    assert "Drawdown breaker: not configured" in _snap(breaker=None)
    assert "Drawdown breaker: disabled" in _snap(breaker=FakeBreaker(halt="0"))


def test_broker_failure_raises_so_the_night_retries():
    with pytest.raises(RuntimeError):
        _snap(fail=True)


# -- options held at the real broker, described only in notify strategy docs
# (the shadow ledger is stock-only; FIG-calls' thesis still said "~5% ITM") --

class Doc:
    def __init__(self, id_, name, thesis, ticker, status="active"):
        self.id, self.name, self.thesis = id_, name, thesis
        self.status = type("S", (), {"value": status})()
        self.position = type("P", (), {"ticker": ticker})()


class FakeStrategies:
    def __init__(self, docs=(), fail=False):
        self.docs, self.fail = list(docs), fail

    def load_all(self, status=None, errors=None):
        if self.fail:
            raise OSError("strategies dir unreadable")
        return self.docs


FIG_DOC = Doc("FIG-calls", "FIG long call reminder ($20C exp 2027-01-15)",
              "Real long FIG $20 call expiring 2027-01-15: 8 contracts (~800 shares "
              "of exposure). The call is back roughly 5% ITM.", "FIG")
ORCL_DOC = Doc("ORCL-leaps", "ORCL LEAPS moneyness and time-decay reminder",
               "Real long ORCL $150 call expiring 2027-01-15: CONFIRMED 3 contracts, "
               "mark ...", "ORCL")
ARCHIVED = Doc("META-cc", "META covered call archive ($650C exp 2026-09-25)",
               "assigned", "META", status="draft")


def _snap_docs(docs, prices, **kw):
    return live_snapshot(broker=FakeBroker([FIG]), data=FakeData(prices),
                         breaker=None, now=NOW, strategies=FakeStrategies(docs, **kw))


def test_doc_options_get_live_moneyness():
    text = _snap_docs([FIG_DOC, ORCL_DOC, ARCHIVED],
                      {"FIG": "18.81", "ORCL": "116.10"})
    assert "Options held at the real broker" in text
    [fig] = [ln for ln in text.splitlines() if ln.startswith("- FIG $20 call")]
    assert "exp 2027-01-15" in fig and "8 contracts per doc FIG-calls (800 sh exposure)" in fig
    assert "FIG last $18.81 -> out of the money by $1.19" in fig
    [orcl] = [ln for ln in text.splitlines() if ln.startswith("- ORCL $150 call")]
    assert "3 contracts" in orcl and "out of the money by $33.90" in orcl
    assert "META" not in text.split("Options held")[1]
    assert "thesis text is stale" in text


def test_no_doc_options_means_no_block_and_unreadable_docs_never_fail():
    assert "Options held" not in _snap_docs([], {"FIG": "18.81"})
    assert "Options held" not in _snap_docs([FIG_DOC], {"FIG": "18.81"}, fail=True)
