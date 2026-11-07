import csv
from datetime import date, timedelta

import pytest

from allpath_trade.research.bars import BarCache, DailyBar
from allpath_trade.research.cli import main
from allpath_trade.research.db import open_readonly
from allpath_trade.research.replay import fires_on_bar, relax_condition, replay_rows
from tests.research_fixtures import DBBuilder, rule, strategy_yaml


def _bar(d, o, h, l, c):
    return DailyBar(date=d, open=o, high=h, low=l, close=c)


def test_relax_condition_drops_position_terms():
    text, relaxed = relax_condition("position_weight < 0.12 and price < 222")
    assert relaxed is True and text == "price < 222"
    text, relaxed = relax_condition("price > 10 and price < 20")
    assert relaxed is False and text == "price > 10 and price < 20"
    assert relax_condition("position_weight == 0")[0] == ""  # nothing left: always true
    assert relax_condition("not position_weight > 0 or price < 5")[0] is None  # unevaluable


def test_fires_on_bar_uses_the_whole_range():
    bar = _bar("2026-09-01", 102, 120, 100, 118)
    assert fires_on_bar("price > 105 and price < 110", bar) == "intraday"
    assert fires_on_bar("price > 115", bar) == "close"
    assert fires_on_bar("price < 99", bar) is None
    assert fires_on_bar("", bar) == "close"


class FakeFetch:
    def __init__(self, bars):
        self.bars = bars
        self.calls = 0

    def __call__(self, ticker, start, end):
        self.calls += 1
        return [b for b in self.bars[ticker] if start <= b.date <= end]


def _series(start, closes, ticker_hl=1.0):
    d = date.fromisoformat(start)
    out = []
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(_bar(d.isoformat(), c, c + ticker_hl, c - ticker_hl, c))
        d += timedelta(days=1)
    return out


def test_bar_cache_is_deterministic(tmp_path):
    fetch = FakeFetch({"NVDA": _series("2026-09-01", [100, 101, 102])})
    cache = BarCache(tmp_path / "bars", fetch=fetch)
    first = cache.get("NVDA", "2026-09-01", "2026-09-03")
    again = BarCache(tmp_path / "bars", fetch=None).get("NVDA", "2026-09-01", "2026-09-03")
    assert first == again and fetch.calls == 1
    with pytest.raises(LookupError):
        BarCache(tmp_path / "bars", fetch=None).get("AMD", "2026-09-01", "2026-09-03")


@pytest.fixture()
def setup(tmp_path):
    b = DBBuilder(tmp_path / "t.db")
    v1 = strategy_yaml("s1", [rule("stop", "price < 95", "sell all"),
                              rule("dip", "position_weight < 0.1 and price < 90")])
    v2 = strategy_yaml("s1", [rule("stop", "price < 98", "sell all"),
                              rule("dip", "position_weight < 0.1 and price < 90"),
                              rule("add", "price > 104", "buy $1000")])
    v3 = strategy_yaml("s1", [rule("stop", "price < 95", "sell all")])
    # vetoed 09-01 (after close): modifies stop 95->98, adds `add`
    veto = b.review(ts="2026-09-01T20:00:00+00:00", status="rejected",
                    resolved_ts="2026-09-01T23:00:00+00:00", old_yaml=v1, new_yaml=v2)
    # applied removal of `dip` on 09-02 (chat)
    rm = b.review(ts="2026-09-02T20:00:00+00:00", source="chat",
                  resolved_ts="2026-09-02T21:00:00+00:00", old_yaml=v1, new_yaml=v3)
    path = b.close()
    closes = [100, 99, 103, 106, 97, 96, 95, 94, 93, 92] + [90 + i for i in range(25)]
    fetch = FakeFetch({"NVDA": _series("2026-09-01", closes)})
    cache = BarCache(tmp_path / "bars", fetch=fetch)
    return path, cache, veto, rm


def _by(rows):
    return {(r["review_id"], r["rule_id"], r["variant"]): r for r in rows}


def test_replay_rows(setup):
    path, cache, veto, rm = setup
    rows = _by(replay_rows(open_readonly(path), cache, asof="2026-10-30"))
    # proposed stop (price < 98): first bar after 09-01 is 09-02 (close 99,
    # low 98 -> not < 98); 09-03 close 103; 09-04 106; 09-07 close 97 -> close
    p = rows[(veto, "stop", "proposed")]
    assert p["decision"] == "vetoed" and p["change"] == "modified"
    assert p["fired_on"] == "2026-09-07" and p["fire_basis"] == "close"
    assert p["fill_px"] == 97
    assert p["side"] == "sell"
    # +5 trading days after 09-07 is 09-14 (close 92); a sell gains when price falls
    assert p["px_5d"] == 92
    assert p["ret_5d"] == pytest.approx(92 / 97 - 1, abs=1e-4)
    assert p["pnl_5d"] == pytest.approx(-(92 / 97 - 1), abs=1e-4)
    # the kept original (price < 95) fires later: 09-09 close 94
    k = rows[(veto, "stop", "kept")]
    assert k["fired_on"] == "2026-09-09"
    # the vetoed new rule `add` (price > 104): 09-03 high 104 is not > 104; 09-04 close 106
    a = rows[(veto, "add", "proposed")]
    assert a["change"] == "added" and a["fired_on"] == "2026-09-04" and a["side"] == "buy"
    assert a["pnl_today"] == pytest.approx(a["ret_today"])
    # removed dip: position term relaxed, price < 90 never closes below 90;
    # low touches 89.x on the 90 bar (low = close - 1) -> intraday
    d = rows[(rm, "dip", "removed")]
    assert d["decision"] == "removed" and d["position_terms_relaxed"] == 1
    assert d["fire_basis"] == "intraday" and d["fired_on"] == "2026-09-15"
    assert d["decision_date"] == "2026-09-02"


def test_cli_replay_rules_offline_uses_cache(setup, tmp_path):
    path, cache, _veto, _rm = setup
    cache.get("NVDA", "2026-08-25", "2026-10-30")  # warm the cache
    out = tmp_path / "out"
    rc = main(["replay-rules", "--db", str(path), "--out", str(out),
               "--bars", str(cache.directory), "--asof", "2026-10-30", "--offline",
               "--sources", "all"])
    assert rc == 0
    with (out / "rule_replay.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert {r["variant"] for r in rows} == {"proposed", "kept", "removed"}
    assert (out / "rule_replay_summary.md").exists()


def test_bar_cache_drops_non_finite_bars(tmp_path):
    bars = _series("2026-09-01", [100, 101]) + [_bar("2026-09-03", 1, 2, 0.5, float("nan"))]
    cache = BarCache(tmp_path / "bars", fetch=FakeFetch({"META": bars}))
    assert [b.date for b in cache.get("META", "2026-09-01", "2026-09-03")] == [
        "2026-09-01", "2026-09-02"]
