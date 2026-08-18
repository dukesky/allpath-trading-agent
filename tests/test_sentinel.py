from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from allpath_trade.broker.base import (
    Account,
    Broker,
    Position,
)
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.execution import ExecutionError
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.sentinel import Sentinel
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.model import RuleState
from allpath_trade.strategy.store import StrategyStore


def strategy_yaml(auth="auto", rule_type="hard", condition="price < 250",
                  action="sell all", status="active", notify_email=True):
    text = f"""
name: "T"
status: {status}
authorization: {auth}
position: {{ticker: AAPL, target_weight: 15%}}
rules:
  - {{id: r1, type: {rule_type}, condition: "{condition}", action: "{action}"}}
"""
    if not notify_email:
        text += "notify_email: false\n"
    return text


class FakeData(DataSource):
    def __init__(self, price="200"):
        self.price = Decimal(price)

    def get_quote(self, ticker):
        return Quote(ticker=ticker, price=self.price,
                     as_of=datetime.now(UTC))

    def get_bars(self, ticker, days=365):
        return []


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def __init__(self, qty="10"):
        self.qty = Decimal(qty)

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(5000),
                       buying_power=Decimal(10000))

    def get_positions(self):
        if self.qty <= 0:
            return []
        return [Position(ticker="AAPL", qty=self.qty,
                         avg_entry_price=Decimal(180),
                         market_value=self.qty * Decimal(200),
                         unrealized_pl=Decimal(0))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


class SpyExecutor:
    def __init__(self, fail=False, raise_exc=None, reject_reasons=None):
        self.fail = fail
        self.raise_exc = raise_exc
        self.reject_reasons = reject_reasons
        self.calls = []

    def execute(self, intent):
        if self.fail:
            raise ExecutionError("boom")
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append(intent)
        from allpath_trade.execution import ExecutionResult
        if self.reject_reasons is not None:
            return ExecutionResult(
                submitted=False, order=None,
                decision=RiskDecision(approved=False, reasons=self.reject_reasons))
        return ExecutionResult(submitted=True, order=None,
                               decision=RiskDecision(approved=True))


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


def make(tmp_path: Path, yaml_text: str, *, price="200", qty="10", fail=False):
    (tmp_path / "t.yaml").write_text(yaml_text)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    executor = SpyExecutor(fail=fail)
    queue = ReviewQueue(conn, executor)
    notifier = SpyNotifier()
    s = Sentinel(store, FakeData(price), FakeBroker(qty), executor, queue, notifier)
    return s, store, executor, queue, notifier


def test_no_trigger_no_noise(tmp_path):
    s, _store, ex, _q, n = make(tmp_path, strategy_yaml(condition="price < 100"))
    report = s.run_once()
    assert report.strategies_checked == 1
    assert report.outcomes == [] and n.sent == [] and ex.calls == []


def test_hard_auto_executes_and_marks_triggered(tmp_path):
    s, store, ex, _q, n = make(tmp_path, strategy_yaml())
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert len(ex.calls) == 1 and ex.calls[0].qty == Decimal(10)
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert len(n.sent) == 1
    # second run: rule stays triggered, nothing happens
    assert s.run_once().outcomes == []


def test_soft_auto_enqueues(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    assert ex.calls == [] and len(q.list()) == 1


def test_confirm_auth_enqueues_hard_rule(tmp_path):
    s, _store, ex, _q, _n = make(tmp_path, strategy_yaml(auth="confirm"))
    assert s.run_once().outcomes[0].disposition == "queued"
    assert ex.calls == []


def test_notify_auth_only_notifies(tmp_path):
    s, _store, ex, q, n = make(tmp_path, strategy_yaml(auth="notify"))
    assert s.run_once().outcomes[0].disposition == "notified"
    assert ex.calls == [] and q.list() == [] and len(n.sent) == 1


def test_no_position_sell_is_skipped(tmp_path):
    s, store, _ex, _q, _n = make(tmp_path, strategy_yaml(), qty="0")
    report = s.run_once()
    assert report.outcomes[0].disposition == "skipped"
    assert store.load("t").rules[0].state == RuleState.TRIGGERED


def test_execution_error_reported_not_raised(tmp_path):
    s, _store, _ex, _q, _n = make(tmp_path, strategy_yaml(), fail=True)
    report = s.run_once()
    assert report.outcomes[0].disposition == "error"
    assert "boom" in report.outcomes[0].detail


def test_bad_quote_collects_error_and_continues(tmp_path):
    class BadData(FakeData):
        def get_quote(self, ticker):
            raise ValueError("no price")

    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor()
    s = Sentinel(store, BadData(), FakeBroker(), ex, ReviewQueue(conn, ex), SpyNotifier())
    report = s.run_once()
    assert report.errors and "no price" in report.errors[0]


def test_draft_strategy_ignored(tmp_path):
    s, *_ = make(tmp_path, strategy_yaml(status="draft"))
    assert s.run_once().strategies_checked == 0


def test_hard_auto_gate_rejected_reported_as_rejected_not_executed(tmp_path):
    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor(reject_reasons=["exceeds max position size"])
    n = SpyNotifier()
    s = Sentinel(store, FakeData(), FakeBroker(), ex, ReviewQueue(conn, ex), n)
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "rejected"
    assert "exceeds max position size" in o.detail
    assert len(n.sent) == 1


def test_hard_auto_unexpected_exception_reported_as_error_not_raised(tmp_path):
    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor(raise_exc=RuntimeError("db connection lost"))
    n = SpyNotifier()
    s = Sentinel(store, FakeData(), FakeBroker(), ex, ReviewQueue(conn, ex), n)
    report = s.run_once()  # must not raise
    [o] = report.outcomes
    assert o.disposition == "error"
    assert "db connection lost" in o.detail
    assert len(n.sent) == 1


def test_one_bad_yaml_does_not_halt_other_strategies(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: x\nstatus: active\n")
    (tmp_path / "t.yaml").write_text(strategy_yaml(condition="price < 100"))
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor()
    s = Sentinel(store, FakeData(), FakeBroker(), ex, ReviewQueue(conn, ex), SpyNotifier())
    report = s.run_once()
    assert report.strategies_checked == 1
    assert report.errors and any("bad.yaml" in e for e in report.errors)


class StubReviewAgent:
    def __init__(self, recommendation="execute", fail=False):
        self.recommendation = recommendation
        self.fail = fail

    def analyze(self, review):
        from allpath_trade.agent.review import ReviewAnalysis
        if self.fail:
            raise RuntimeError("llm down")
        return ReviewAnalysis(recommendation=self.recommendation, reasoning="because")


def test_auto_soft_with_agent_execute_recommendation(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    assert report.outcomes[0].disposition == "executed"
    assert len(ex.calls) == 1
    row = q.get(1)
    assert row["status"] == "approved" and "because" in row["agent_analysis"]


def test_auto_soft_with_agent_skip_recommendation(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent("skip")
    report = s.run_once()
    assert report.outcomes[0].disposition == "skipped"
    assert ex.calls == []
    assert q.get(1)["status"] == "rejected"


def test_confirm_with_agent_attaches_analysis_stays_queued(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(auth="confirm"))
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    row = q.get(1)
    assert row["status"] == "pending" and row["agent_analysis"]
    assert ex.calls == []


def test_agent_failure_leaves_trigger_queued(tmp_path):
    s, _store, _ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent(fail=True)
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    assert "review failed" in report.outcomes[0].detail
    assert q.get(1)["status"] == "pending"


def test_auto_soft_agent_execute_but_executor_fails_reports_error(tmp_path):
    s, _store, _ex, q, n = make(tmp_path, strategy_yaml(rule_type="soft"), fail=True)
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "error"
    assert "execution failed" in o.detail
    row = q.get(1)
    assert row["status"] == "approved"       # truthfully claimed
    assert "error" in (row["execution_result"] or "")
    assert len(n.sent) == 1                  # user still notified


def test_auto_soft_with_unparseable_analysis_stays_pending(tmp_path):
    from allpath_trade.agent.review import ReviewAnalysis

    class UnparseableStub:
        def analyze(self, review):
            return ReviewAnalysis(recommendation="skip",
                                  reasoning="unparseable analysis: garbage")

    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = UnparseableStub()
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "queued"
    assert "unparseable" in o.detail
    row = q.get(1)
    assert row["status"] == "pending"
    assert ex.calls == []


def test_auto_soft_agent_execute_but_gate_rejects_reports_rejected(tmp_path):
    s, _store, ex, q, n = make(tmp_path, strategy_yaml(rule_type="soft"))
    ex.reject_reasons = ["exceeds max position size"]
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "rejected"
    assert "risk gate" in o.detail and "exceeds max position size" in o.detail
    row = q.get(1)
    assert row["status"] == "approved"       # agent's decision truthfully recorded
    assert len(n.sent) == 1


def test_confirm_detail_includes_recommendation_and_reasoning(tmp_path):
    s, _store, _ex, _q, _n = make(tmp_path, strategy_yaml(auth="confirm"))
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    [o] = report.outcomes
    assert "execute" in o.detail and "because" in o.detail


def test_sentinel_records_observations(tmp_path):
    from allpath_trade.memory.observations import ObservationLog

    s, _store, _ex, q, _n = make(tmp_path, strategy_yaml())
    s.observations = ObservationLog(q._conn)
    s.run_once()
    rows = s.observations.recent()
    assert rows and "t/r1" in rows[0]["text"] and rows[0]["subject"] == "AAPL"


def test_strategy_error_recorded_under_distinct_source(tmp_path):
    # Per-strategy failures (bad quote, etc.) must never share the
    # "sentinel" source with real rule triggers — the daily digest counts
    # "sentinel"-sourced rows as trigger count, so an error logged under
    # that source would silently inflate it.
    from allpath_trade.memory.observations import ObservationLog

    class BadData(FakeData):
        def get_quote(self, ticker):
            raise ValueError("no price")

    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor()
    s = Sentinel(store, BadData(), FakeBroker(), ex, ReviewQueue(conn, ex), SpyNotifier())
    s.observations = ObservationLog(conn)
    s.run_once()
    rows = s.observations.recent()
    assert rows and rows[0]["source"] == "sentinel_error"
    assert all(r["source"] != "sentinel" for r in rows)


def test_hard_auto_executed_notification_uses_order_result_event(tmp_path):
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml())
    s.run_once()
    [(subject, body)] = n.sent
    assert "AAPL" in subject and "order submitted" in subject
    assert "sell" in body and "submitted" in body
    assert "http" not in body.lower() and "<" not in body


def test_notify_auth_notification_uses_rule_triggered_event(tmp_path):
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(auth="notify"))
    s.run_once()
    [(subject, body)] = n.sent
    assert "AAPL" in subject and "rule r1 triggered" in subject
    assert "t" in body and "r1" in body and "notified" in body


def test_confirm_auth_queued_notification_uses_review_queued_event(tmp_path):
    s, _store, _ex, q, n = make(tmp_path, strategy_yaml(auth="confirm"))
    s.run_once()
    [(subject, body)] = n.sent
    assert "AAPL" in subject and "waiting for your approval" in subject
    review_id = q.list()[0]["id"]
    assert f"#{review_id}" in body and "sell all" in body and "t" in body


def test_confirm_auth_queued_notification_includes_trigger_price(tmp_path):
    # Part B: the price the rule actually triggered on (FakeData's price
    # default, "200") shows up in the notification body, honestly labeled
    # as the price at trigger time -- not a second, separately fetched
    # "current" quote.
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(auth="confirm"), price="200")
    s.run_once()
    [(_subject, body)] = n.sent
    assert "Price at trigger: $200.00" in body


def test_confirm_auth_queued_notification_includes_est_shares_for_notional_intent(tmp_path):
    s, _store, _ex, _q, n = make(
        tmp_path, strategy_yaml(auth="confirm", condition="price > 0", action="buy $500"),
        price="200")
    s.run_once()
    [(_subject, body)] = n.sent
    assert "Est. size: ~2.50 shares at that price" in body


def test_queued_notification_carries_no_link_when_web_base_url_unset(tmp_path):
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(auth="confirm"))
    assert s.web_base_url == ""
    s.run_once()
    [(_subject, body)] = n.sent
    assert "Review & approve" not in body
    assert "http" not in body.lower()


def test_queued_notification_carries_an_approve_link_when_web_base_url_set(tmp_path):
    s, _store, _ex, q, n = make(tmp_path, strategy_yaml(auth="confirm"))
    s.web_base_url = "http://192.168.1.20:8791"
    s.run_once()
    [(_subject, body)] = n.sent
    review_id = q.list()[0]["id"]
    assert f"Review & approve: http://192.168.1.20:8791/a/{review_id}?k=" in body
    # the token in the link must be the review's real (single-use) token --
    # not logged/echoed anywhere else, but it must validate.
    url_line = next(line for line in body.splitlines() if line.startswith("Review & approve:"))
    token = url_line.rsplit("k=", 1)[1]
    assert q.validate_token(review_id, token) is not None


def test_confirm_with_agent_queued_notification_includes_recommendation(tmp_path):
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(auth="confirm"))
    s.review_agent = StubReviewAgent("execute")
    s.run_once()
    [(subject, body)] = n.sent
    assert "waiting for your approval" in subject
    assert "agent recommends: execute" in body and "because" in body


def test_no_position_sell_skipped_sends_notification(tmp_path):
    # A rule the user wrote fired even though there was nothing to act on
    # (no position to sell) — silence would leave them with no way to know
    # it triggered at all, so this must still notify, briefly.
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(), qty="0")
    s.run_once()
    [(subject, body)] = n.sent
    assert "AAPL" in subject and "rule r1 triggered" in subject
    assert "skipped" in body


def test_agent_skip_recommendation_sends_notification(tmp_path):
    # Same reasoning as above: the agent reviewed and chose not to act, but
    # the rule still fired and the review is already resolved (rejected) —
    # this is exactly the case where the user has no other way to learn it.
    s, _store, ex, _q, n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent("skip")
    s.run_once()
    assert ex.calls == []
    [(subject, body)] = n.sent
    assert "AAPL" in subject and "rule r1 triggered" in subject
    assert "skipped" in body


def test_notify_email_false_suppresses_notification_but_still_executes(tmp_path):
    # The flag gates the email only -- the disposition (execute/queue/record)
    # must happen exactly as if notify_email were true.
    s, store, ex, _q, n = make(tmp_path, strategy_yaml(notify_email=False))
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert len(ex.calls) == 1
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert n.sent == []


def test_notify_email_false_suppresses_notification_but_still_queues(tmp_path):
    s, _store, ex, q, n = make(tmp_path, strategy_yaml(auth="confirm", notify_email=False))
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    assert len(q.list()) == 1
    assert ex.calls == []
    assert n.sent == []


def test_notify_email_true_still_notifies(tmp_path):
    # Sanity check the default (and explicit true) is unaffected.
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(notify_email=True))
    s.run_once()
    assert len(n.sent) == 1


# ---------------------------------------------------------------------------
# Telegram push (notify/dispatch.py) -- both the queued-review buttons leg
# and the auto-executed order_result receipt leg.
# ---------------------------------------------------------------------------

class FakeTelegramAPI:
    def __init__(self, token):
        self.token = token
        self.sent = []

    def send_message(self, chat_id, html, reply_markup=None):
        self.sent.append((chat_id, html, reply_markup))
        return True


def _paired_sentinel(tmp_path, yaml_text, *, price="200", qty="10", fail=False):
    from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState

    s, store, ex, q, n = make(tmp_path, yaml_text, price=price, qty=qty, fail=fail)
    app_state = AppState(q._conn)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    s.app_state = app_state
    s.telegram_bot_token = "fake-bot-token"
    return s, store, ex, q, n


def test_confirm_auth_queued_notification_pushes_telegram_buttons(tmp_path, monkeypatch):
    instances = []

    class RecordingAPI(FakeTelegramAPI):
        def __init__(self, token):
            super().__init__(token)
            instances.append(self)

    from allpath_trade.notify import dispatch

    monkeypatch.setattr(dispatch, "TelegramAPI", RecordingAPI)
    s, _store, _ex, q, _n = _paired_sentinel(tmp_path, strategy_yaml(auth="confirm"))

    s.run_once()

    [api] = instances
    assert len(api.sent) == 1
    chat_id, _html, markup = api.sent[0]
    assert chat_id == "111"
    review_id = q.list()[0]["id"]
    [[approve, reject]] = markup["inline_keyboard"]
    assert approve["callback_data"].startswith(f"rv:approve:{review_id}:")
    assert reject["callback_data"].startswith(f"rv:reject:{review_id}:")


def test_hard_auto_execution_receipt_pushes_telegram_no_buttons(tmp_path, monkeypatch):
    instances = []

    class RecordingAPI(FakeTelegramAPI):
        def __init__(self, token):
            super().__init__(token)
            instances.append(self)

    from allpath_trade.notify import dispatch

    monkeypatch.setattr(dispatch, "TelegramAPI", RecordingAPI)
    s, *_ = _paired_sentinel(tmp_path, strategy_yaml())

    s.run_once()

    [api] = instances
    assert len(api.sent) == 1
    chat_id, html, markup = api.sent[0]
    assert chat_id == "111"
    assert markup is None
    assert "submitted" in html


def test_telegram_push_noop_when_unpaired(tmp_path, monkeypatch):
    instances = []

    class RecordingAPI(FakeTelegramAPI):
        def __init__(self, token):
            super().__init__(token)
            instances.append(self)

    from allpath_trade.notify import dispatch

    monkeypatch.setattr(dispatch, "TelegramAPI", RecordingAPI)
    s, *_ = make(tmp_path, strategy_yaml())  # no app_state/telegram_bot_token set

    s.run_once()  # must not raise

    assert instances == []


def test_hard_auto_gate_rejected_notification_uses_order_result_event(tmp_path):
    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor(reject_reasons=["exceeds max position size"])
    n = SpyNotifier()
    s = Sentinel(store, FakeData(), FakeBroker(), ex, ReviewQueue(conn, ex), n)
    s.run_once()
    [(subject, body)] = n.sent
    assert "order not submitted" in subject
    assert "exceeds max position size" in body
