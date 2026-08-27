from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from allpath_trade.broker.base import (
    Account,
    Broker,
    Order,
    OrderSide,
    OrderStatus,
    Position,
)
from allpath_trade.broker.options_mcp import OptionPick, OptionsBackendError
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

    def __init__(self, qty="10", extra_positions=None, open_orders=None):
        self.qty = Decimal(qty)
        # Task 6: OCC-symbol option positions a test wants held alongside
        # (or instead of) the AAPL stock position above -- used by
        # close_options and expiry-sweep tests. Empty by default, so every
        # pre-existing call site (none of which passes this) is unaffected.
        self.extra_positions = list(extra_positions or [])
        # Finding 2b: open orders `get_orders(open_only=True)` returns --
        # used by the expiry sweep's already-selling dedup. Empty by
        # default (matches the prior hardcoded `return []`), so every
        # pre-existing call site is unaffected.
        self.open_orders = list(open_orders or [])
        self.get_orders_raises = False

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(5000),
                       buying_power=Decimal(10000))

    def get_positions(self):
        positions = []
        if self.qty > 0:
            positions.append(Position(ticker="AAPL", qty=self.qty,
                             avg_entry_price=Decimal(180),
                             market_value=self.qty * Decimal(200),
                             unrealized_pl=Decimal(0)))
        positions.extend(self.extra_positions)
        return positions

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        if self.get_orders_raises:
            raise RuntimeError("broker unavailable")
        return self.open_orders

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
        # Task 6: option calls are recorded separately from `calls` --
        # `OptionIntent` and `OrderIntent` are different shapes, and every
        # pre-existing test asserting on `calls` must keep seeing exactly
        # the stock-path intents it always did.
        self.option_calls = []

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

    def execute_option(self, intent):
        if self.fail:
            raise ExecutionError("boom")
        if self.raise_exc is not None:
            raise self.raise_exc
        self.option_calls.append(intent)
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


class FakeOptionsBackend:
    """Minimal `OptionsBackend` fake for sentinel tests -- unlike
    tests/test_execution.py's fake of the same name, sentinel tests always
    route order placement through `SpyExecutor.execute_option` (never the
    real `Executor`), so this fake only needs `pick_contract` to actually
    do anything; `place_option_order` is never called from these tests and
    stays a stub."""

    def __init__(self, pick=None, pick_fail=False):
        self.pick = pick
        self.pick_fail = pick_fail
        self.pick_calls = []

    def pick_contract(self, underlying, right, min_dte, otm_pct, budget, spot):
        self.pick_calls.append((underlying, right, min_dte, otm_pct, budget, spot))
        if self.pick_fail:
            raise OptionsBackendError("mcp down")
        return self.pick

    def place_option_order(self, occ_symbol, side, qty, position_intent):
        raise NotImplementedError

    def stop(self):
        pass


def _occ_symbol(root: str, expiry: date, right: str = "C", strike=200) -> str:
    return f"{root}{expiry.strftime('%y%m%d')}{right}{int(strike * 1000):08d}"


def _occ_position(occ_symbol: str, qty: str = "1") -> Position:
    q = Decimal(qty)
    return Position(ticker=occ_symbol, qty=q, avg_entry_price=Decimal(5),
                    market_value=q * Decimal(500), unrealized_pl=Decimal(0))


def make(tmp_path: Path, yaml_text: str, *, price="200", qty="10", fail=False,
         account="paper"):
    (tmp_path / "t.yaml").write_text(yaml_text)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    executor = SpyExecutor(fail=fail)
    queue = ReviewQueue(conn, executor, account=account)
    notifier = SpyNotifier()
    s = Sentinel(store, FakeData(price), FakeBroker(qty), executor, queue, notifier,
                 account=account)
    return s, store, executor, queue, notifier


def make_option(tmp_path: Path, yaml_text: str, *, backend, price="200", qty="10",
                account="paper", extra_positions=None, broker=None):
    """Like `make`, but wires an `options_backend` (a `FakeOptionsBackend`,
    or None to test the disabled path) and lets the caller seed OCC-symbol
    positions alongside the usual AAPL stock position.

    `broker` lets a Finding-2b test pass a pre-built `FakeBroker` (e.g. one
    seeded with `open_orders=` or `get_orders_raises=True`) instead of the
    plain one this helper would otherwise construct from `qty`/
    `extra_positions`."""
    (tmp_path / "t.yaml").write_text(yaml_text)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    executor = SpyExecutor()
    queue = ReviewQueue(conn, executor, account=account)
    notifier = SpyNotifier()
    if broker is None:
        broker = FakeBroker(qty, extra_positions=extra_positions)
    s = Sentinel(store, FakeData(price), broker, executor, queue, notifier,
                account=account, options_backend=backend)
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


# ---------------------------------------------------------------------------
# C3: a shadow Sentinel's `detail` is the string that reaches the notification
# body, the TriggerOutcome, and (through it) the run report -- "submitted" in
# any of them claims an order was routed, which the shadow ledger never does.
# ---------------------------------------------------------------------------

def test_hard_auto_shadow_detail_and_notification_say_recorded(tmp_path):
    s, _store, ex, _q, n = make(tmp_path, strategy_yaml(), account="shadow")
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert o.detail == "recorded"
    assert len(ex.calls) == 1
    subject, body = n.sent[0]
    assert "submitted" not in subject
    assert "submitted" not in body
    assert "recorded in your shadow ledger" in body


def test_hard_auto_paper_detail_still_says_submitted(tmp_path):
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(), account="paper")
    report = s.run_once()
    [o] = report.outcomes
    assert o.detail == "submitted"
    assert "order submitted" in n.sent[0][0]


def test_auto_soft_agent_approved_shadow_detail_says_recorded(tmp_path):
    s, _store, _ex, _q, n = make(tmp_path, strategy_yaml(rule_type="soft"),
                                 account="shadow")
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert o.detail == "agent-approved; recorded"
    assert "submitted" not in n.sent[-1][1]


def test_auto_soft_agent_approved_paper_detail_still_says_submitted(tmp_path):
    s, _store, _ex, _q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    assert report.outcomes[0].detail == "agent-approved; submitted"


# -- setup-wizard T1: an unconfigured paper broker --


def test_run_once_on_an_unconfigured_broker_reports_one_error_and_checks_nothing(tmp_path):
    # `serve` now starts with no Alpaca keys, so the scheduler's sentinel
    # pass can reach a paper account whose broker is the placeholder. That
    # is a setup state, not a broken environment: it must read as one clear
    # line, never as a raw "setup failed: <exception>" dump, and no strategy
    # may be evaluated (every evaluation needs live equity/positions).
    from allpath_trade.broker.unconfigured import UnconfiguredBroker

    s, store, ex, _q, n = make(tmp_path, strategy_yaml())
    s.broker = UnconfiguredBroker()

    report = s.run_once()

    assert report.errors == ["paper broker not configured"]
    assert report.strategies_checked == 0
    assert report.outcomes == []
    assert ex.calls == [] and n.sent == []
    assert store.load("t").rules[0].state == RuleState.ARMED


# ---------------------------------------------------------------------------
# Task 7: the drawdown circuit breaker (risk/breaker.py) wired into
# run_once -- tripped before any strategy is evaluated, so a demoted auto
# strategy is evaluated as confirm on this same pass; optional (None by
# default) so every Sentinel constructed without one keeps behaving exactly
# as it did before this breaker existed.
# ---------------------------------------------------------------------------

class EquityBroker(FakeBroker):
    """FakeBroker with a configurable equity, for breaker tests only --
    FakeBroker itself stays a fixed 10000 (test_web_dashboard.py and others
    depend on that exact value)."""

    def __init__(self, equity, qty="10", extra_positions=None):
        super().__init__(qty, extra_positions=extra_positions)
        self._equity = Decimal(equity)

    def get_account(self):
        return Account(equity=self._equity, cash=Decimal(5000),
                       buying_power=self._equity)


def _breaker_make(tmp_path, yaml_text, *, price="200", equity="80000",
                  qty="10", halt_pct="0.15", peak="100000", account="paper"):
    from allpath_trade.risk.breaker import DrawdownBreaker
    from allpath_trade.store.app_state import AppState

    (tmp_path / "t.yaml").write_text(yaml_text)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    executor = SpyExecutor()
    queue = ReviewQueue(conn, executor, account=account)
    notifier = SpyNotifier()
    app_state = AppState(conn)
    if peak is not None:
        app_state.set(f"drawdown_peak:{account}", str(peak))
    broker = EquityBroker(equity, qty=qty)
    breaker = DrawdownBreaker(app_state, store, Decimal(halt_pct), account)
    s = Sentinel(store, FakeData(price), broker, executor, queue, notifier,
                account=account, app_state=app_state, breaker=breaker)
    return s, store, executor, queue, notifier, breaker, app_state


def test_breaker_trip_halts_notifies_and_records(tmp_path):
    # Equity 80,000 against a pre-seeded peak of 100,000 is a 20% drawdown
    # -- above the default 15% halt_pct -- so this pass trips. One auto/hard
    # strategy whose rule fires at the fake price (200 < 250).
    s, store, ex, q, n, _breaker, _app_state = _breaker_make(
        tmp_path, strategy_yaml(auth="auto"))

    report = s.run_once()

    assert any("drawdown breaker" in e for e in report.errors)
    assert n.sent
    subject, _body = n.sent[0]
    assert "TRADING HALTED" in subject
    # The demotion (inside breaker.check) already landed before load_all
    # ran, so the strategy is evaluated as confirm on this same pass: the
    # triggering rule must land in the review queue, not execute.
    assert ex.calls == []
    assert len(q.list()) == 1
    assert store.load("t").authorization.value == "confirm"


def test_breaker_trip_sends_even_when_notify_email_is_off(tmp_path):
    # The breaker alert is an account-level halt, not a per-strategy
    # notification preference -- it must reach the operator even when
    # every strategy on file has notify_email: false.
    s, _store, _ex, _q, n, _breaker, _app_state = _breaker_make(
        tmp_path, strategy_yaml(auth="auto", notify_email=False))

    s.run_once()

    assert n.sent
    subject, _body = n.sent[0]
    assert "TRADING HALTED" in subject


def test_breaker_trip_uses_breaker_observation_source(tmp_path):
    from allpath_trade.memory.observations import ObservationLog

    s, _store, _ex, _q, _n, _breaker, app_state = _breaker_make(
        tmp_path, strategy_yaml(auth="auto"))
    observations = ObservationLog(app_state._conn, account="paper")
    s.observations = observations

    s.run_once()

    sources = [row["source"] for row in observations.recent(limit=10)]
    # "breaker", not "sentinel" -- so the daily digest, which counts
    # "sentinel" rows as rule triggers, never miscounts a breaker trip as
    # one (sentinel.py:122-129's own rationale for "sentinel_error").
    assert "breaker" in sources


def test_no_breaker_means_no_behavior_change(tmp_path):
    # A Sentinel constructed without breaker= (every pre-existing call
    # site) runs exactly as it did before this breaker existed.
    s, store, ex, _q, n = make(tmp_path, strategy_yaml())
    assert s.breaker is None

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "executed"
    assert len(ex.calls) == 1
    assert not any("drawdown breaker" in e for e in report.errors)
    assert len(n.sent) == 1
    assert store.load("t").rules[0].state == RuleState.TRIGGERED


def test_tripped_breaker_does_not_realert(tmp_path):
    s, _store, _ex, _q, n, _breaker, _app_state = _breaker_make(
        tmp_path, strategy_yaml(auth="auto"))

    first = s.run_once()
    assert any("drawdown breaker" in e for e in first.errors)
    first_sent = len(n.sent)
    assert first_sent >= 1

    second = s.run_once()

    assert not any("drawdown breaker" in e for e in second.errors)
    assert not any("TRADING HALTED" in subject for subject, _body in n.sent[first_sent:])


def _breaker_make_option(tmp_path, yaml_text, *, backend, price="200", equity="80000",
                         qty="10", halt_pct="0.15", peak="100000", account="paper",
                         extra_positions=None):
    """Like `_breaker_make`, but wires an `options_backend` and lets the
    caller seed OCC-symbol positions -- used by Finding 1's regression
    test below."""
    from allpath_trade.risk.breaker import DrawdownBreaker
    from allpath_trade.store.app_state import AppState

    (tmp_path / "t.yaml").write_text(yaml_text)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    executor = SpyExecutor()
    queue = ReviewQueue(conn, executor, account=account)
    notifier = SpyNotifier()
    app_state = AppState(conn)
    if peak is not None:
        app_state.set(f"drawdown_peak:{account}", str(peak))
    broker = EquityBroker(equity, qty=qty, extra_positions=extra_positions)
    breaker = DrawdownBreaker(app_state, store, Decimal(halt_pct), account)
    s = Sentinel(store, FakeData(price), broker, executor, queue, notifier,
                account=account, app_state=app_state, breaker=breaker,
                options_backend=backend)
    return s, store, executor, queue, notifier, breaker, app_state


def test_breaker_demoted_auto_option_strategy_still_loads_buy_skipped_close_executes(tmp_path):
    # Finding 1 regression. Before the fix: DrawdownBreaker.check demotes
    # every `auto` strategy to `confirm` via StrategyStore.set_authorization
    # (risk/breaker.py), which does NOT re-validate the strategy. The very
    # next line in run_once, `self.strategies.load_all(...)`, used to
    # re-enforce "option actions require authorization: auto + type: hard"
    # on every load (strategy/loader.py) -- so the now-demoted file would
    # raise StrategyValidationError forever after, and load_all treats a
    # bad file as "skip it entirely", silently dropping EVERY rule in it,
    # including a close_options stop-loss, precisely during the drawdown
    # the breaker exists to protect against.
    #
    # After the fix: loading no longer enforces that check (only authoring
    # does -- see loader.py's `authoring` param), so the strategy keeps
    # loading; sentinel.py's `_dispatch_option` is the new runtime last
    # line of defense that keeps its buy_call rule from firing anyway
    # (skipped, not executed), while its close_options rule -- risk-
    # reducing, and exempt from the authorization gate by design -- still
    # executes.
    future_expiry = date.today() + timedelta(days=90)
    aapl_call = _occ_symbol("AAPL", future_expiry)
    yaml_text = """
name: "T"
status: active
authorization: auto
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: entry, type: hard, condition: "price < 250", action: "buy_call $500"}
  - {id: exit, type: hard, condition: "price < 250", action: "close_options"}
"""
    backend = FakeOptionsBackend(pick=_PICK)
    s, store, ex, _q, _n, _breaker, _app_state = _breaker_make_option(
        tmp_path, yaml_text, backend=backend, extra_positions=[_occ_position(aapl_call)])

    report = s.run_once()

    # The breaker tripped and demoted this strategy on this same pass.
    assert any("drawdown breaker" in e for e in report.errors)
    assert store.load("t").authorization.value == "confirm"

    # The strategy still loaded and BOTH its rules were evaluated -- the
    # bug this regresses against would have made `load_all` skip this file
    # entirely, leaving `report.outcomes` empty.
    outcomes = {o.rule_id: o for o in report.outcomes}
    assert set(outcomes) == {"entry", "exit"}
    assert outcomes["entry"].disposition == "skipped"
    assert "authorization: auto" in outcomes["entry"].detail
    assert outcomes["exit"].disposition == "executed"
    assert aapl_call in outcomes["exit"].detail

    # The buy never reached the executor; the close did.
    assert len(ex.option_calls) == 1
    assert ex.option_calls[0].side.value == "sell"
    assert ex.option_calls[0].occ_symbol == aapl_call


# ---------------------------------------------------------------------------
# Task 6: option dispatch (buy_call/buy_put/close_options) and the DTE<=1
# expiry safety sweep. `SpyExecutor.execute_option` records intents the same
# way `.execute` already does for stock intents -- these tests exercise
# `_dispatch_option`/`_dispatch_close_options`/`_run_expiry_sweep`, not the
# real `Executor` (that's tests/test_execution.py's job).
# ---------------------------------------------------------------------------

_PICK = OptionPick(
    occ_symbol="AAPL260101C00204000", expiry=date(2026, 1, 1),
    strike=Decimal("204"), ask=Decimal("2.50"), qty=2, est_premium=Decimal("500"))


def test_buy_call_executes_with_defaults_applied_when_action_omits_them(tmp_path):
    backend = FakeOptionsBackend(pick=_PICK)
    s, store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(action="buy_call $500"), backend=backend)

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "executed"
    # Defaults (dte 7, otm 2%) are applied by the sentinel, not the parser --
    # spec.min_dte/otm_pct are None on the parsed action here.
    assert backend.pick_calls == [
        ("AAPL", "call", 7, Decimal("0.02"), Decimal("500"), Decimal("200"))]
    [intent] = ex.option_calls
    assert intent.underlying == "AAPL"
    assert intent.right == "call"
    assert intent.occ_symbol == "AAPL260101C00204000"
    assert intent.side.value == "buy"
    assert intent.qty == 2
    assert intent.est_premium == Decimal("500")
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert len(n.sent) == 1


def test_buy_put_explicit_dte_and_otm_are_passed_through_not_defaulted(tmp_path):
    put_pick = OptionPick(occ_symbol="AAPL260101P00196000", expiry=date(2026, 1, 1),
                          strike=Decimal("196"), ask=Decimal("2.10"), qty=1,
                          est_premium=Decimal("210"))
    backend = FakeOptionsBackend(pick=put_pick)
    s, *_ = make_option(
        tmp_path, strategy_yaml(action="buy_put $500 dte>=10 otm=3%"), backend=backend)

    s.run_once()

    assert backend.pick_calls == [
        ("AAPL", "put", 10, Decimal("0.03"), Decimal("500"), Decimal("200"))]


def test_buy_call_no_affordable_contract_is_skipped(tmp_path):
    backend = FakeOptionsBackend(pick=None)
    s, store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(action="buy_call $500"), backend=backend)

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "skipped"
    assert "no affordable option contract" in o.detail
    assert ex.option_calls == []
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert len(n.sent) == 1


def test_buy_call_backend_error_reported_as_error_not_raised(tmp_path):
    backend = FakeOptionsBackend(pick_fail=True)
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(action="buy_call $500"), backend=backend)

    report = s.run_once()  # must not raise

    [o] = report.outcomes
    assert o.disposition == "error"
    assert "mcp down" in o.detail
    assert ex.option_calls == []
    assert len(n.sent) == 1


def test_buy_call_without_options_backend_is_error_not_crash(tmp_path):
    s, _store, ex, _q, n = make(tmp_path, strategy_yaml(action="buy_call $500"))

    report = s.run_once()  # must not raise

    [o] = report.outcomes
    assert o.disposition == "error"
    assert o.detail == "options trading disabled"
    assert ex.calls == [] and ex.option_calls == []
    assert len(n.sent) == 1


def test_buy_call_rejected_by_executor_is_rejected_not_executed(tmp_path):
    backend = FakeOptionsBackend(pick=_PICK)
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(action="buy_call $500"), backend=backend)
    ex.reject_reasons = ["exceeds max_options_weight"]

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "rejected"
    assert "exceeds max_options_weight" in o.detail
    assert len(n.sent) == 1


def test_close_options_closes_only_matching_underlying_positions(tmp_path):
    # Far-future expiry -- must stay outside the DTE<=1 expiry sweep's
    # window (also active in this test, since it also needs an
    # options_backend), so this test isolates close_options's own filtering.
    future_expiry = date.today() + timedelta(days=90)
    aapl_call = _occ_symbol("AAPL", future_expiry)
    other_call = _occ_symbol("MSFT", future_expiry)
    backend = FakeOptionsBackend()
    s, store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(action="close_options"), backend=backend,
        extra_positions=[_occ_position(aapl_call), _occ_position(other_call)])

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "executed"
    assert aapl_call in o.detail
    [intent] = ex.option_calls
    assert intent.occ_symbol == aapl_call
    assert intent.underlying == "AAPL"
    assert intent.side.value == "sell"
    assert intent.qty == 1
    assert intent.est_premium == Decimal(0)
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert len(n.sent) == 1  # one order_result receipt for the one close


def test_close_options_one_bad_qty_position_does_not_abort_the_batch(tmp_path):
    # Finding 5: `OptionIntent.qty` requires >= 1 -- a position whose qty
    # truncates to 0 via `int(p.qty)` (e.g. a stray 0.5-contract position
    # from a bad broker payload, which should never happen but must be
    # handled defensively) used to raise a `ValidationError` OUTSIDE the
    # per-position try/except, abandoning every remaining position in this
    # strategy's close_options batch. The fix moves construction inside the
    # try -- the bad position is recorded as an issue, and the good
    # position right after it still gets closed.
    future_expiry = date.today() + timedelta(days=90)
    bad_call = _occ_symbol("AAPL", future_expiry, strike=190)
    good_call = _occ_symbol("AAPL", future_expiry, strike=210)
    backend = FakeOptionsBackend()
    s, store, ex, _q, _n = make_option(
        tmp_path, strategy_yaml(action="close_options"), backend=backend,
        extra_positions=[_occ_position(bad_call, qty="0.5"),
                         _occ_position(good_call, qty="1")])

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "executed"  # at least one position closed
    assert bad_call in o.detail
    assert good_call in o.detail
    [intent] = ex.option_calls
    assert intent.occ_symbol == good_call
    assert intent.side.value == "sell"
    assert store.load("t").rules[0].state == RuleState.TRIGGERED


def test_close_options_with_no_matching_positions_is_skipped(tmp_path):
    backend = FakeOptionsBackend()
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(action="close_options"), backend=backend)

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "skipped"
    assert "no option positions to close" in o.detail
    assert ex.option_calls == []
    assert len(n.sent) == 1


def test_close_options_without_options_backend_is_error_not_crash(tmp_path):
    s, _store, ex, _q, n = make(tmp_path, strategy_yaml(action="close_options"))

    report = s.run_once()  # must not raise

    [o] = report.outcomes
    assert o.disposition == "error"
    assert o.detail == "options trading disabled"
    assert ex.calls == [] and ex.option_calls == []
    assert len(n.sent) == 1


def test_close_options_on_soft_rule_is_skipped_not_executed(tmp_path):
    # Finding 1b defense in depth: the loader's authoring-time validation
    # normally guarantees every option action sits on a `type: hard` rule,
    # but a strategy YAML written directly to disk (bypassing draft_strategy/
    # propose_strategy_revision) can still reach the sentinel with a soft
    # rule. `_dispatch_option`'s close_options branch must independently
    # refuse to execute in that case rather than trusting the loader's
    # guarantee alone -- closes ignore `authorization` by design, but never
    # `type`.
    future_expiry = date.today() + timedelta(days=90)
    aapl_call = _occ_symbol("AAPL", future_expiry)
    yaml_text = """
name: "T"
status: active
authorization: auto
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: soft, condition: "price < 250", action: "close_options"}
"""
    backend = FakeOptionsBackend()
    s, _store, ex, _q, n = make_option(
        tmp_path, yaml_text, backend=backend, extra_positions=[_occ_position(aapl_call)])

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "skipped"
    assert "type: hard" in o.detail
    assert ex.option_calls == []
    assert len(n.sent) == 1


def test_expiry_sweep_closes_dte_le_1_and_leaves_dte_gt_1(tmp_path):
    today = date.today()
    expiring = _occ_symbol("AAPL", today + timedelta(days=1))  # DTE == 1
    safe = _occ_symbol("AAPL", today + timedelta(days=5))       # DTE == 5
    backend = FakeOptionsBackend()
    # No rule fires (condition never true) -- isolates the sweep from the
    # strategy loop entirely.
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend,
        extra_positions=[_occ_position(expiring), _occ_position(safe)])

    report = s.run_once()

    assert report.outcomes == []  # no rule triggered
    [intent] = ex.option_calls
    assert intent.occ_symbol == expiring
    assert intent.side.value == "sell"
    assert intent.reason == "expiry safety sweep (DTE<=1)"
    assert len(n.sent) == 1  # one receipt for the one closed position


def test_expiry_sweep_closes_a_position_expiring_today(tmp_path):
    today_expiry = _occ_symbol("AAPL", date.today())  # DTE == 0
    backend = FakeOptionsBackend()
    s, _store, ex, _q, _n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend,
        extra_positions=[_occ_position(today_expiry)])

    s.run_once()

    assert [i.occ_symbol for i in ex.option_calls] == [today_expiry]


def test_expiry_sweep_absent_when_options_backend_is_none(tmp_path):
    today = date.today()
    expiring = _occ_symbol("AAPL", today)
    # `make_option` with backend=None: an OCC position at DTE 0 is present,
    # but `run_once` must never call `_run_expiry_sweep` at all when
    # `options_backend` is None (not just no-op inside it).
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=None,
        extra_positions=[_occ_position(expiring)])

    report = s.run_once()

    assert ex.option_calls == []
    assert n.sent == []
    assert report.outcomes == []


def test_expiry_sweep_uses_options_sweep_observation_source(tmp_path):
    from allpath_trade.memory.observations import ObservationLog

    today = date.today()
    expiring = _occ_symbol("AAPL", today)
    backend = FakeOptionsBackend()
    s, _store, _ex, q, _n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend,
        extra_positions=[_occ_position(expiring)])
    s.observations = ObservationLog(q._conn)

    s.run_once()

    rows = s.observations.recent()
    assert rows and rows[0]["source"] == "options_sweep"
    assert all(r["source"] != "sentinel" for r in rows)


def test_expiry_sweep_rejected_close_is_recorded_in_report_errors(tmp_path):
    expiring = _occ_symbol("AAPL", date.today())
    backend = FakeOptionsBackend()
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend,
        extra_positions=[_occ_position(expiring)])
    ex.reject_reasons = ["daily trade limit reached"]

    report = s.run_once()

    assert any("expiry sweep" in e and expiring in e for e in report.errors)
    assert len(n.sent) == 1


def test_expiry_sweep_exception_isolated_into_report_errors(tmp_path):
    expiring = _occ_symbol("AAPL", date.today())
    backend = FakeOptionsBackend()
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend,
        extra_positions=[_occ_position(expiring)])
    ex.raise_exc = ExecutionError("mcp down")

    report = s.run_once()  # must not raise

    assert any("expiry sweep" in e and "mcp down" in e for e in report.errors)
    assert n.sent == []


# ---------------------------------------------------------------------------
# Finding 2b: the expiry sweep must not submit a second sell-to-close on a
# position that already has an open sell order (e.g. from a close_options
# rule that fired earlier in this same pass, or an unresolved order left
# over from a prior pass).
# ---------------------------------------------------------------------------

def _open_sell_order(occ_symbol: str) -> Order:
    return Order(id="o1", ticker=occ_symbol, side=OrderSide.SELL, qty=Decimal(1),
                notional=None, status=OrderStatus.SUBMITTED, filled_qty=Decimal(0),
                filled_avg_price=None, submitted_at=datetime.now(UTC))


def test_expiry_sweep_skips_position_with_open_sell_order(tmp_path):
    expiring = _occ_symbol("AAPL", date.today())
    backend = FakeOptionsBackend()
    broker = FakeBroker(extra_positions=[_occ_position(expiring)],
                        open_orders=[_open_sell_order(expiring)])
    s, _store, ex, _q, n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend, broker=broker)

    report = s.run_once()

    assert ex.option_calls == []
    assert n.sent == []
    assert report.outcomes == []
    assert report.errors == []


def test_expiry_sweep_still_closes_positions_without_an_open_sell_order(tmp_path):
    # An open order on a DIFFERENT symbol must not suppress the sweep for
    # the one that actually needs closing.
    expiring = _occ_symbol("AAPL", date.today())
    other = _occ_symbol("MSFT", date.today())
    backend = FakeOptionsBackend()
    broker = FakeBroker(extra_positions=[_occ_position(expiring)],
                        open_orders=[_open_sell_order(other)])
    s, _store, ex, _q, _n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend, broker=broker)

    s.run_once()

    assert [i.occ_symbol for i in ex.option_calls] == [expiring]


def test_expiry_sweep_ignores_open_buy_orders_for_the_same_symbol(tmp_path):
    # Only an open SELL order suppresses the sweep -- a (nonsensical in
    # practice, but defensive) open BUY on the same OCC symbol must not.
    expiring = _occ_symbol("AAPL", date.today())
    open_buy = Order(id="o2", ticker=expiring, side=OrderSide.BUY, qty=Decimal(1),
                     notional=None, status=OrderStatus.SUBMITTED, filled_qty=Decimal(0),
                     filled_avg_price=None, submitted_at=datetime.now(UTC))
    backend = FakeOptionsBackend()
    broker = FakeBroker(extra_positions=[_occ_position(expiring)], open_orders=[open_buy])
    s, _store, ex, _q, _n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend, broker=broker)

    s.run_once()

    assert [i.occ_symbol for i in ex.option_calls] == [expiring]


def test_expiry_sweep_get_orders_failure_falls_back_to_sweeping_everything(tmp_path):
    # Best-effort filter: a broker failure fetching open orders must not
    # skip the sweep itself -- proceed as if nothing has an open sell.
    expiring = _occ_symbol("AAPL", date.today())
    backend = FakeOptionsBackend()
    broker = FakeBroker(extra_positions=[_occ_position(expiring)])
    broker.get_orders_raises = True
    s, _store, ex, _q, _n = make_option(
        tmp_path, strategy_yaml(condition="price < 0"), backend=backend, broker=broker)

    s.run_once()  # must not raise

    assert [i.occ_symbol for i in ex.option_calls] == [expiring]
