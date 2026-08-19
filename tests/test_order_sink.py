from typing import ClassVar

import pytest

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.data.base import DataSource
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.web.order_sink import QueueingOrderSink
from tests.test_sentinel import FakeBroker, FakeData


class RaisingData(DataSource):
    """Conforms to the real DataSource ABC so a signature drift fails this
    suite instead of hiding behind a phantom method — every call raises."""

    def get_quote(self, ticker: str):
        raise RuntimeError("no quote")

    def get_bars(self, ticker: str, days: int = 365):
        raise RuntimeError("no bars")


@pytest.fixture
def sink(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None)
    broker = FakeBroker()
    return QueueingOrderSink(queue, RiskGate(RiskLimits()), broker, FakeData(),
                             TradeJournal(conn), conversation_id=7), queue


def test_proposal_is_queued_not_executed(sink):
    s, queue = sink
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="1000",
                         reason="rebalance")
    message = s.propose(intent)
    rows = queue.list("pending")
    assert len(rows) == 1
    assert rows[0]["source"] == "chat"
    assert rows[0]["conversation_id"] == 7
    assert "#" in message


def test_risk_preview_is_recorded(sink):
    s, queue = sink
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="999999",
                         reason="too big")
    s.propose(intent)
    row = queue.list("pending")[0]
    assert "max_order_value" in row["risk_preview"]


def test_preview_failure_still_queues_the_proposal(sink, monkeypatch):
    s, queue = sink

    def boom(*a, **k):
        raise RuntimeError("no quote")

    # qty (not notional) so the preview actually calls get_quote and hits
    # the stubbed failure.
    monkeypatch.setattr(s.data, "get_quote", boom)
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty="10",
                         reason="x")
    s.propose(intent)
    assert len(queue.list("pending")) == 1
    row = queue.list("pending")[0]
    assert "could not be checked" in row["risk_preview"]


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return True


class FakeTelegramAPI:
    instances: ClassVar[list] = []

    def __init__(self, token):
        self.token = token
        self.sent = []
        FakeTelegramAPI.instances.append(self)

    def send_message(self, chat_id, html, reply_markup=None):
        self.sent.append((chat_id, html, reply_markup))
        return True


def test_propose_notifies_email_and_telegram_when_configured(tmp_path, monkeypatch):
    from allpath_trade.notify import dispatch
    from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState

    FakeTelegramAPI.instances = []
    monkeypatch.setattr(dispatch, "TelegramAPI", FakeTelegramAPI)

    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None)
    app_state = AppState(conn)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    notifier = SpyNotifier()
    s = QueueingOrderSink(queue, RiskGate(RiskLimits()), FakeBroker(), FakeData(),
                          TradeJournal(conn), notifier=notifier, app_state=app_state,
                          telegram_bot_token="fake-bot-token")

    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="1000", reason="rebalance")
    s.propose(intent)

    assert len(notifier.sent) == 1
    assert "AAPL" in notifier.sent[0][0]
    [api] = FakeTelegramAPI.instances
    assert len(api.sent) == 1
    chat_id, _html, markup = api.sent[0]
    assert chat_id == "111"
    assert markup["inline_keyboard"][0][0]["callback_data"].startswith("rv:approve:")


def test_propose_without_notifier_or_telegram_stays_silent(sink):
    # Default construction (no notifier/app_state/telegram_bot_token) --
    # matching every other test in this file -- must still queue with zero
    # notification attempts, same as before this feature existed.
    s, queue = sink
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="1000", reason="x")
    s.propose(intent)  # must not raise
    assert len(queue.list("pending")) == 1


def test_propose_notification_failure_does_not_break_the_proposal(tmp_path):
    class RaisingNotifier:
        def send(self, subject, body):
            raise RuntimeError("smtp down")

    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None)
    s = QueueingOrderSink(queue, RiskGate(RiskLimits()), FakeBroker(), FakeData(),
                          TradeJournal(conn), notifier=RaisingNotifier())

    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="1000", reason="x")
    out = s.propose(intent)  # must not raise

    assert "#" in out
    assert len(queue.list("pending")) == 1


def test_notional_preview_needs_no_quote(tmp_path):
    """A notional-sized order converts straight to order_value (no shares to
    price), so the gate needs no quote at all — this must produce a real
    pass/reject preview even when the data source is completely unusable,
    not fall through to "could not be checked" like Finding 1's bug did."""
    conn = connect(tmp_path / "t2.db")
    queue = ReviewQueue(conn, None)
    s = QueueingOrderSink(queue, RiskGate(RiskLimits()), FakeBroker(),
                          RaisingData(), TradeJournal(conn))
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="999999",
                         reason="too big")
    s.propose(intent)
    row = queue.list("pending")[0]
    assert "could not be checked" not in row["risk_preview"]
    assert "max_order_value" in row["risk_preview"]
