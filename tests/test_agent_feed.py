"""Machine-readable surfaces for an external executor agent: `--json` on
`status` / `reviews list`, and the AGENT_WEBHOOK_URL event channel."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from allpath_trade.cli import main
from allpath_trade.config import Settings
from allpath_trade.notify import events
from allpath_trade.notify.agent import AgentWebhookNotifier, send_agent_only
from allpath_trade.notify.base import MultiNotifier, Notifier
from allpath_trade.notify.email import EmailNotifier, build_notifier
from allpath_trade.store.reviews import ReviewHandle
from tests.test_cli import FakeBroker


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_urlopen(monkeypatch, status=200):
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append({"url": req.full_url, "data": req.data,
                         "headers": dict(req.header_items()), "timeout": timeout})
        return _FakeResponse(status)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


# --- CLI --json ----------------------------------------------------------

def test_status_json_is_parseable_and_uses_string_decimals(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = main(["status", "--json"], broker_factory=lambda settings: FakeBroker())
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["account"] == "paper"
    assert data["broker"] == "fake" and data["is_paper"] is True
    assert data["equity"] == "10000" and data["cash"] == "4000"
    assert data["positions"] == [{"ticker": "AAPL", "qty": "5",
                                  "avg_entry_price": "190",
                                  "market_value": "1000", "unrealized_pl": "50"}]
    assert data["recent_trades"] == []


def test_status_json_includes_recent_trades(tmp_path, capsys, monkeypatch):
    from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
    from allpath_trade.risk.gate import RiskDecision
    from allpath_trade.store.db import connect
    from allpath_trade.store.journal import TradeJournal

    monkeypatch.chdir(tmp_path)
    journal = TradeJournal(connect(tmp_path / "allpath-trade.db"))
    intent = OrderIntent(ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1), reason="dip")
    order = Order(id="o1", ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1),
                 notional=None, status=OrderStatus.SUBMITTED, filled_qty=Decimal(0),
                 filled_avg_price=None, submitted_at="2026-08-09T20:27:00+00:00")
    journal.record(intent, RiskDecision(approved=True), order)

    main(["status", "--json"], broker_factory=lambda settings: FakeBroker())
    trades = json.loads(capsys.readouterr().out)["recent_trades"]
    assert len(trades) == 1
    assert trades[0]["ticker"] == "TSLA" and trades[0]["side"] == "buy"
    assert trades[0]["status"] == "submitted"
    assert isinstance(trades[0]["risk_reasons"], list)


def test_status_json_broker_error_is_json_and_exit_1(tmp_path, capsys, monkeypatch):
    from tests.test_cli import RaisingBroker

    monkeypatch.chdir(tmp_path)
    code = main(["status", "--json"], broker_factory=lambda settings: RaisingBroker())
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "error" in data


def test_reviews_list_json_empty_is_empty_array(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["reviews", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_reviews_list_json_rows_are_scoped_and_secret_free(tmp_path, capsys, monkeypatch):
    from allpath_trade.store.db import connect
    from allpath_trade.store.reviews import ReviewQueue

    monkeypatch.chdir(tmp_path)
    conn = connect(tmp_path / "allpath-trade.db")
    paper_q = ReviewQueue(conn, None)
    handle = paper_q.add(strategy_id="s1", rule_id="r1", ticker="AAPL", rule_type="soft",
                         condition="price < 1", action="buy $100", snapshot={}, intent=None)
    paper_q.attach_analysis(handle, json.dumps({"recommendation": "approve",
                                                "reasoning": "fine"}))
    ReviewQueue(conn, None, account="shadow").add(
        strategy_id="s2", rule_id="r2", ticker="TSLA", rule_type="soft",
        condition="c", action="SHDW", snapshot={}, intent=None)
    conn.close()

    assert main(["reviews", "list", "--json"]) == 0
    raw = capsys.readouterr().out
    rows = json.loads(raw)
    assert [r["ticker"] for r in rows] == ["AAPL"]
    row = rows[0]
    assert row["id"] == int(handle) and row["account"] == "paper"
    assert row["status"] == "pending" and row["kind"] == "order"
    assert row["strategy_id"] == "s1" and row["action"] == "buy $100"
    assert row["agent_analysis"] == {"recommendation": "approve", "reasoning": "fine"}
    assert "approval_token_hash" not in row
    assert handle.token not in raw


def test_reviews_list_json_keeps_unparseable_analysis_as_text(tmp_path, capsys, monkeypatch):
    from allpath_trade.store.db import connect
    from allpath_trade.store.reviews import ReviewQueue

    monkeypatch.chdir(tmp_path)
    conn = connect(tmp_path / "allpath-trade.db")
    q = ReviewQueue(conn, None)
    h = q.add(strategy_id="s1", rule_id="r1", ticker="AAPL", rule_type="soft",
              condition="c", action="a", snapshot={}, intent=None)
    q.attach_analysis(h, "not json")
    conn.close()

    main(["reviews", "list", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["agent_analysis"] == "not json"


# --- structured events on subjects --------------------------------------

def test_review_queued_subject_carries_secret_free_event():
    handle = ReviewHandle(42)
    handle.token = "SECRETTOKEN"
    url = events.approve_link("https://example.test", handle)
    subject, body = events.review_queued(
        account="paper", review_id=handle, ticker="MSFT", action="buy_call $1200",
        strategy_id="msft-swing", approve_url=url, kind="option_order")
    assert "SECRETTOKEN" in body  # the human body still carries the link
    assert subject.event == {
        "type": "review_queued", "account": "paper", "review_id": 42,
        "kind": "option_order", "ticker": "MSFT", "strategy_id": "msft-swing",
        "action": "buy_call $1200",
        "next": "allpath-trade reviews --account paper list --json"}
    assert type(subject.event["review_id"]) is int
    assert "SECRETTOKEN" not in json.dumps(subject.event)


@pytest.mark.parametrize(("build", "etype"), [
    (lambda: events.rule_triggered(account="paper", strategy_id="s", rule_id="r",
                                   ticker="NVDA", condition="price < 1",
                                   disposition="queued"), "rule_triggered"),
    (lambda: events.order_result(account="shadow", ticker="META", side="sell",
                                 submitted=True, detail="d"), "order_result"),
    (lambda: events.drawdown_halt(account="paper", peak=Decimal(100), equity=Decimal(80),
                                  drawdown=Decimal("0.2"), demoted=["s"]), "drawdown_halt"),
    (lambda: events.daily_digest(account="paper", triggers=1, trades=2, pending=3),
     "daily_digest"),
    (lambda: events.daily_report(account="shadow", date="2026-09-25", summary="s",
                                 body="b"), "daily_report"),
])
def test_every_builder_tags_its_subject(build, etype):
    subject, _ = build()
    assert subject.event["type"] == etype
    assert subject.event["account"] in ("paper", "shadow")
    json.dumps(subject.event)  # always serializable
    assert isinstance(subject, str)


def test_order_result_event_flags_shadow_as_recorded_only():
    subject, _ = events.order_result(account="shadow", ticker="META", side="sell",
                                     submitted=True, detail="d",
                                     filled_qty=Decimal(15),
                                     filled_avg_price=Decimal("660.91"))
    assert subject.event["recorded_only"] is True
    assert subject.event["filled_qty"] == "15"
    assert subject.event["filled_avg_price"] == "660.91"
    paper, _ = events.order_result(account="paper", ticker="NVDA", side="buy",
                                   submitted=True, detail="d")
    assert paper.event["recorded_only"] is False


# --- webhook channel -----------------------------------------------------

def test_webhook_posts_event_json_without_body(monkeypatch):
    captured = _capture_urlopen(monkeypatch)
    handle = ReviewHandle(7)
    handle.token = "SECRETTOKEN"
    subject, body = events.review_queued(
        account="paper", review_id=handle, ticker="NVDA", action="buy $100",
        strategy_id="s", approve_url=events.approve_link("https://x.test", handle))
    n = AgentWebhookNotifier("https://hooks.test/allpath", token="BEARER1",
                             clock=lambda: "2026-09-26T16:00:00+00:00")
    assert n.send(subject, body) is True

    (call,) = captured
    assert call["url"] == "https://hooks.test/allpath"
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Bearer BEARER1"
    assert headers["x-allpath-event"] == "review_queued"
    assert call["timeout"] == 10
    payload = json.loads(call["data"])
    assert payload["v"] == 1 and payload["ts"] == "2026-09-26T16:00:00+00:00"
    assert payload["type"] == "review_queued" and payload["review_id"] == 7
    assert payload["subject"] == str(subject)
    assert "SECRETTOKEN" not in call["data"].decode()
    assert "Item #7 is waiting" not in call["data"].decode()


def test_webhook_without_token_sends_no_authorization(monkeypatch):
    captured = _capture_urlopen(monkeypatch)
    AgentWebhookNotifier("https://hooks.test/x").send("plain subject", "body")
    headers = {k.lower() for k in captured[0]["headers"]}
    assert "authorization" not in headers
    payload = json.loads(captured[0]["data"])
    assert payload["type"] == "notification" and payload["subject"] == "plain subject"


def test_webhook_failure_returns_false_and_scrubs_token(monkeypatch, capsys):
    def boom(req, timeout=None):
        raise OSError("connect failed Bearer BEARER1")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert AgentWebhookNotifier("https://hooks.test/x", token="BEARER1").send("s", "b") is False
    err = capsys.readouterr().err
    assert "agent webhook send failed" in err and "BEARER1" not in err


def test_webhook_non_2xx_returns_false(monkeypatch):
    _capture_urlopen(monkeypatch, status=500)
    assert AgentWebhookNotifier("https://hooks.test/x").send("s", "b") is False


def test_build_notifier_adds_agent_channel(monkeypatch):
    s = Settings(_env_file=None, smtp_host="smtp.test", notify_to="me@test",
                 agent_webhook_url="https://hooks.test/x", agent_webhook_token="t")
    n = build_notifier(s)
    assert isinstance(n, MultiNotifier)
    assert any(isinstance(c, EmailNotifier) for c in n.children)
    agent = [c for c in n.children if isinstance(c, AgentWebhookNotifier)]
    assert agent and agent[0].url == "https://hooks.test/x" and agent[0].token == "t"


def test_build_notifier_agent_only():
    n = build_notifier(Settings(_env_file=None, agent_webhook_url="https://hooks.test/x"))
    assert isinstance(n, AgentWebhookNotifier)


def test_agent_webhook_url_needs_scheme():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_webhook_url="hooks.test/x")


class _Spy(Notifier):
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append(subject)
        return True


class _SpyAgent(AgentWebhookNotifier):
    def __init__(self):
        super().__init__("https://hooks.test/x")
        self.sent = []

    def send(self, subject, body):
        self.sent.append(subject)
        return True


def test_send_agent_only_skips_human_channels():
    human, agent = _Spy(), _SpyAgent()
    send_agent_only(MultiNotifier([human, agent]), "s", "b")
    assert human.sent == [] and agent.sent == ["s"]
    send_agent_only(agent, "s2", "b")
    assert agent.sent == ["s", "s2"]
    send_agent_only(human, "s3", "b")
    send_agent_only(None, "s4", "b")
    assert human.sent == []


def test_muted_review_still_reaches_agent_channel(tmp_path):
    from allpath_trade.notify.dispatch import notify_review_queued
    from allpath_trade.store.db import connect
    from allpath_trade.store.reviews import ReviewQueue

    q = ReviewQueue(connect(tmp_path / "t.db"), None)
    rid = q.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                condition="c", action="a", snapshot={}, intent=None)
    human, agent = _Spy(), _SpyAgent()
    notify_review_queued(queue=q, notifier=MultiNotifier([human, agent]), app_state=None,
                         telegram_bot_token="", review_id=rid, subject="subj",
                         body="b", account="paper", notify_email=False)
    assert human.sent == [] and agent.sent == ["subj"]


# --- non-ASCII titles (the shadow "order recorded — place it yourself") ---

def test_header_safe_encodes_non_latin1_as_rfc2047():
    import base64

    from allpath_trade.notify.base import header_safe

    assert header_safe("plain ascii") == "plain ascii"
    encoded = header_safe("order recorded — place it yourself")
    assert encoded.startswith("=?UTF-8?B?") and encoded.endswith("?=")
    assert encoded.isascii()
    decoded = base64.b64decode(encoded[len("=?UTF-8?B?"):-2]).decode()
    assert decoded == "order recorded — place it yourself"


def _loopback_post_server():
    import http.server
    import threading

    received: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/topic", received


@pytest.mark.parametrize("make", [
    lambda url: __import__("allpath_trade.notify.ntfy", fromlist=["x"]).NtfyNotifier(url),
    lambda url: AgentWebhookNotifier(url),
])
def test_shadow_order_subject_is_deliverable_over_real_http(make):
    # Real urllib + a loopback server: the latin-1 header encoding failure
    # only shows up in http.client, not in a mocked urlopen.
    srv, url, received = _loopback_post_server()
    try:
        subject, body = events.order_result(account="shadow", ticker="META",
                                            side="sell", submitted=True, detail="d")
        assert "—" in subject
        assert make(url).send(subject, body) is True
        assert received[0]["Title"].startswith("=?UTF-8?B?")
    finally:
        srv.shutdown()
