from __future__ import annotations

import urllib.error
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from allpath_trade.publish import build_daily_digest, publish_digest


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


class _FakeBroker:
    def __init__(self, equity=Decimal("108423.17"), history=None):
        self._equity = equity
        self._history = history if history is not None else []

    def get_account(self):
        return SimpleNamespace(equity=self._equity)

    def get_equity_history(self, days):
        return self._history


class _RaisingHistoryBroker(_FakeBroker):
    def get_equity_history(self, days):
        raise RuntimeError("yfinance is down")


class _FakeJournal:
    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []

    def recent(self, limit=50):
        return self._rows[:limit]


class _FakeReports:
    def __init__(self, rows=None):
        self._rows = rows if rows is not None else {}

    def get(self, date):
        return self._rows.get(date)


class _FakeQueue:
    def __init__(self, pending=None):
        self._pending = pending if pending is not None else []

    def list(self, status="pending"):
        return self._pending


def _components(*, equity=Decimal("108423.17"), history=None, trades=None,
                report=None, pending=None, date_et="2026-08-18"):
    broker = _FakeBroker(equity=equity, history=history)
    journal = _FakeJournal(rows=trades or [])
    reports = _FakeReports(rows={date_et: report} if report is not None else {})
    queue = _FakeQueue(pending=pending or [])
    return SimpleNamespace(broker=broker, journal=journal, reports=reports, queue=queue)


def _trade_row(ticker="AAPL", side="buy", qty="10", notional=None, status="filled",
               ts="2026-08-18T14:30:00+00:00", filled_at=None, filled_avg_price=None,
               filled_qty="0", reason="momentum breakout"):
    return {
        "ticker": ticker, "side": side, "qty": qty, "notional": notional,
        "status": status, "ts": ts, "filled_at": filled_at,
        "filled_avg_price": filled_avg_price, "filled_qty": filled_qty,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# build_daily_digest -- shape
# ---------------------------------------------------------------------------

def test_digest_has_the_documented_shape_and_decimal_money_as_str():
    components = _components(equity=Decimal("108423.17"))

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["date"] == "2026-08-18"
    assert digest["equity"] == "108423.17"
    assert isinstance(digest["equity"], str)  # Decimal -> str, never float
    assert digest["trades"] == []
    assert digest["reflection_summary"] == ""
    assert digest["reflection_body"] == ""
    assert digest["pending_proposals"] == 0
    assert digest["day_change"] is None
    assert digest["day_change_pct"] is None


def test_digest_trades_are_filtered_to_the_requested_et_calendar_day():
    # 2026-08-18 14:30 UTC = 2026-08-18 10:30 ET (EDT, UTC-4) -- inside the day.
    in_day = _trade_row(ticker="AAPL", ts="2026-08-18T14:30:00+00:00")
    # 2026-08-19 14:30 UTC = 2026-08-19 10:30 ET -- a different day entirely.
    other_day = _trade_row(ticker="MSFT", ts="2026-08-19T14:30:00+00:00")
    components = _components(trades=[other_day, in_day])

    digest = build_daily_digest(components, "2026-08-18")

    assert [t["ticker"] for t in digest["trades"]] == ["AAPL"]


def test_digest_trades_boundary_case_early_utc_timestamp_belongs_to_prior_et_day():
    # 2026-08-18 02:00 UTC = 2026-08-17 22:00 ET (EDT, UTC-4) -- still the
    # PRIOR ET calendar day even though the UTC date already reads 08-18.
    # This is exactly the boundary window the ET conversion has to get
    # right: a naive string-prefix compare on the UTC timestamp would
    # wrongly attribute this trade to 2026-08-18.
    boundary_trade = _trade_row(ticker="TSLA", ts="2026-08-18T02:00:00+00:00")
    components = _components(trades=[boundary_trade])

    digest_17 = build_daily_digest(components, "2026-08-17")
    digest_18 = build_daily_digest(components, "2026-08-18")

    assert [t["ticker"] for t in digest_17["trades"]] == ["TSLA"]
    assert digest_18["trades"] == []


def test_digest_trades_render_fill_fields_verbatim():
    filled = _trade_row(
        ticker="AAPL", status="filled", ts="2026-08-18T14:30:00+00:00",
        filled_at="2026-08-18T14:30:05+00:00", filled_avg_price="231.50",
        filled_qty="10", reason="momentum breakout")
    components = _components(trades=[filled])

    digest = build_daily_digest(components, "2026-08-18")

    [row] = digest["trades"]
    assert row == {
        "ticker": "AAPL", "side": "buy", "qty": "10", "notional": None,
        "status": "filled", "submitted_ts": "2026-08-18T14:30:00+00:00",
        "filled_at": "2026-08-18T14:30:05+00:00", "filled_avg_price": "231.50",
        "filled_qty": "10", "reason": "momentum breakout",
    }


def test_digest_no_report_for_the_day_yields_empty_strings():
    components = _components(report=None, date_et="2026-08-18")

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["reflection_summary"] == ""
    assert digest["reflection_body"] == ""


def test_digest_carries_the_days_reflection_report():
    report = {"summary": "Tightened AAPL entry rules.", "body": "Full write-up..."}
    components = _components(report=report, date_et="2026-08-18")

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["reflection_summary"] == "Tightened AAPL entry rules."
    assert digest["reflection_body"] == "Full write-up..."


def test_digest_reflection_body_is_capped_at_20000_chars():
    long_body = "x" * 30_000
    report = {"summary": "s", "body": long_body}
    components = _components(report=report, date_et="2026-08-18")

    digest = build_daily_digest(components, "2026-08-18")

    assert len(digest["reflection_body"]) == 20_000


def test_digest_pending_proposals_counts_only_strategy_revision_kind():
    pending = [
        {"kind": "strategy_revision"},
        {"kind": "strategy_revision"},
        {"kind": "order"},
        {"kind": "shadow_edit"},
    ]
    components = _components(pending=pending)

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["pending_proposals"] == 2


def test_digest_day_change_is_null_when_history_is_empty():
    components = _components(equity=Decimal(1000), history=[])

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["day_change"] is None
    assert digest["day_change_pct"] is None


def test_digest_day_change_is_null_when_history_lookup_raises():
    components = _components(equity=Decimal(1000))
    components.broker = _RaisingHistoryBroker(equity=Decimal(1000))

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["day_change"] is None
    assert digest["day_change_pct"] is None


def test_digest_day_change_is_computed_against_the_prior_days_close():
    # Prior close (2026-08-17, ET) was 100000; current equity is 101000 ->
    # +1000 / +1.0%.
    history = [(datetime(2026, 8, 17, 20, 0, tzinfo=UTC), Decimal(100000))]
    components = _components(equity=Decimal(101000), history=history)

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["day_change"] == "1000"
    assert digest["day_change_pct"] == 1.0


def test_digest_day_change_ignores_a_same_day_history_point():
    # A history point already dated the SAME ET day as the digest must not
    # be treated as "yesterday's close" -- only a strictly earlier point
    # counts as "previous".
    history = [
        (datetime(2026, 8, 16, 20, 0, tzinfo=UTC), Decimal(99000)),
        (datetime(2026, 8, 18, 14, 0, tzinfo=UTC), Decimal(105000)),
    ]
    components = _components(equity=Decimal(101000), history=history)

    digest = build_daily_digest(components, "2026-08-18")

    assert digest["day_change"] == "2000"  # 101000 - 99000


# ---------------------------------------------------------------------------
# publish_digest -- never raises, scrubs the token, POSTs JSON
# ---------------------------------------------------------------------------

def test_publish_digest_posts_json_with_bearer_auth_and_returns_true(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        captured["content_type"] = req.get_header("Content-type")
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _FakeResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok = publish_digest("https://trading.all-path.com/api/journal", "secret-tok",
                        {"date": "2026-08-18", "equity": "1000"})

    assert ok is True
    assert captured["url"] == "https://trading.all-path.com/api/journal"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer secret-tok"
    assert captured["content_type"] == "application/json"
    assert b'"date": "2026-08-18"' in captured["data"]
    assert captured["timeout"] == 15


def test_publish_digest_non_2xx_returns_false_and_never_raises(monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResponse(500))

    ok = publish_digest("https://trading.all-path.com/api/journal", "secret-tok", {})

    assert ok is False
    err = capsys.readouterr().err
    assert "[publish] failed" in err


def test_publish_digest_transport_error_returns_false_and_never_raises(monkeypatch, capsys):
    def raise_error(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", raise_error)

    ok = publish_digest("https://trading.all-path.com/api/journal", "secret-tok", {})

    assert ok is False
    assert "[publish] failed" in capsys.readouterr().err


def test_publish_digest_token_never_appears_in_stderr_even_when_the_failure_embeds_it(
        monkeypatch, capsys):
    token = "sk-super-secret-9f8e7d6c"

    def raise_error_with_token_in_message(req, timeout=None):
        # Simulate a failure whose text happens to embed the token, e.g. a
        # transport error echoing back the failing URL/headers.
        raise RuntimeError(f"connection to https://x/?auth={token} failed")

    monkeypatch.setattr("urllib.request.urlopen", raise_error_with_token_in_message)

    ok = publish_digest("https://trading.all-path.com/api/journal", token, {})

    assert ok is False
    captured = capsys.readouterr()
    assert token not in captured.err
    assert token not in captured.out
    assert "***" in captured.err


def test_publish_digest_scheme_less_url_returns_false_and_never_raises():
    # Settings validates publish_url before it reaches here, but the
    # never-raises contract has to hold regardless of where the URL came
    # from -- urllib.request.Request("garbage", ...) raises ValueError if
    # construction weren't inside the try, same as NtfyNotifier's own test.
    ok = publish_digest("garbage", "tok", {"date": "2026-08-18"})

    assert ok is False
