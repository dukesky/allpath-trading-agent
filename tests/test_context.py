from allpath_trade.agent.context import DEFAULT_IDENTITY, build_system_prompt, load_identity
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.store import StrategyStore
from tests.test_sentinel import FakeBroker

STRAT = """
name: "T"
status: active
authorization: confirm
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


def test_load_identity_falls_back_to_default(tmp_path):
    assert load_identity(tmp_path / "nope.md") == DEFAULT_IDENTITY
    custom = tmp_path / "IDENTITY.md"
    custom.write_text("# custom identity")
    assert load_identity(custom) == "# custom identity"


def test_system_prompt_snapshot(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "t.yaml").write_text(STRAT)
    conn = connect(tmp_path / "db.sqlite")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None))
    assert prompt.startswith("IDENT")
    assert "AAPL" in prompt          # position
    assert "t" in prompt and "confirm" in prompt  # strategy line
    assert "pending reviews: 0" in prompt


def test_default_identity_mentions_boundaries():
    text = DEFAULT_IDENTITY.lower()
    assert "risk gate" in text and "confirm" in text


def test_system_prompt_includes_market_mechanics_knowledge(tmp_path):
    # Motivating bug: the agent didn't know DAY orders submitted after hours
    # queue for the next open, and speculated instead of stating the fact.
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "t.yaml").write_text(STRAT)
    conn = connect(tmp_path / "db.sqlite")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None))
    assert "DAY market orders" in prompt
    assert "09:30-16:00 ET" in prompt
    assert "next market open" in prompt
    assert prompt.startswith("IDENT")  # still the frozen prefix identity requires


def test_market_mechanics_note_is_not_baked_into_identity_md_defaults(tmp_path):
    # IDENTITY.md is user-editable content (deliverable 4): a user who
    # replaces it entirely must still get the market-mechanics fact, since
    # it comes from build_system_prompt, not from load_identity's fallback.
    custom = tmp_path / "IDENTITY.md"
    custom.write_text("# a user's own identity, no market mechanics text")
    identity = load_identity(custom)
    assert "DAY market orders" not in identity


def test_system_prompt_mentions_non_trading_days(tmp_path):
    # M4: a DAY order queued outside market hours also queues on a
    # non-trading day (weekend/holiday), not just outside 09:30-16:00 ET on
    # an otherwise-open day -- the note must say so.
    (tmp_path / "strategies").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None))
    assert "non-trading day" in prompt


def test_system_prompt_recent_trade_line_labels_submission_not_fill(tmp_path):
    # I1: this is the system-prompt snapshot every chat/reflection session
    # opens with -- the exact surface the original bug (a Sunday-evening
    # submission mislabeled as the fill, 17 hours off) came from. It must
    # reuse the same "submitted"-labeled, status-driven formatting as
    # get_portfolio, not a bare unlabeled timestamp.
    from decimal import Decimal

    from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
    from allpath_trade.risk.gate import RiskDecision

    (tmp_path / "strategies").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    journal = TradeJournal(conn)
    intent = OrderIntent(ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1), reason="dip")
    order = Order(id="o1", ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1),
                 notional=None, status=OrderStatus.SUBMITTED, filled_qty=Decimal(0),
                 filled_avg_price=None, submitted_at="2026-08-09T20:27:00+00:00")
    journal.record(intent, RiskDecision(approved=True), order)

    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=journal,
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None))
    assert "trade: submitted " in prompt
    assert "fill pending" in prompt


def test_system_prompt_includes_memory_sections(tmp_path):
    from allpath_trade.memory.store import MemoryStore

    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "t.yaml").write_text(STRAT)
    conn = connect(tmp_path / "db.sqlite")
    memory = MemoryStore(tmp_path / "memory", conn)
    memory.apply("profile", None, "add", text="Prefers dividend stocks")
    memory.apply("stock", "AAPL", "add", text="Earnings vol ±8%")
    memory.apply("stock", "ZZZZ", "add", text="unrelated ticker")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None), memory=memory)
    assert "Prefers dividend stocks" in prompt
    assert "Earnings vol" in prompt          # AAPL is held + in strategy
    assert "unrelated ticker" not in prompt  # ZZZZ not relevant


# -- shadow-dual-active T4 review Important 1: which account is this? ------

def test_system_prompt_omits_account_section_when_not_given(tmp_path):
    # No caller has been wired to a specific account bundle yet unless it
    # opts in (chat/terminal are still Task 5's job) -- `account=None`
    # (the default) must render nothing extra, not a broken/empty section.
    (tmp_path / "strategies").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None))
    assert "ACCOUNT:" not in prompt


def test_system_prompt_paper_account_section(tmp_path):
    (tmp_path / "strategies").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None), account="paper")
    assert "ACCOUNT: paper" in prompt
    assert "Alpaca paper sandbox" in prompt
    assert "actually executed (simulated)" in prompt
    # Paper's wording must not carry shadow's "recorded, not executed"
    # framing -- the two sections are mutually exclusive per prompt.
    assert "LOCAL LEDGER" not in prompt


def test_system_prompt_shadow_account_section(tmp_path):
    (tmp_path / "strategies").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None), account="shadow")
    assert "ACCOUNT: shadow" in prompt
    assert "LOCAL LEDGER" in prompt
    assert "user's real brokerage" in prompt
    assert "RECORDED here" in prompt
    assert "user executes them manually" in prompt
    assert "place this order" in prompt
    # Shadow's wording must not carry paper's "actually executed" framing.
    assert "Alpaca paper sandbox" not in prompt


def test_system_prompt_includes_screenshot_import_guidance(tmp_path):
    # setup-wizard T5 (spec ③): the shadow ledger is meant to be filled
    # from a brokerage screenshot, and the model must restate what it read
    # before writing anything -- never guess an unreadable number.
    (tmp_path / "strategies").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    prompt = build_system_prompt(
        identity="IDENT", broker=FakeBroker(),
        journal=TradeJournal(conn),
        strategies=StrategyStore(tmp_path / "strategies", conn),
        queue=ReviewQueue(conn, executor=None), account="shadow")
    assert "## Screenshots of positions" in prompt
    assert "shadow_set_position" in prompt and "shadow_set_cash" in prompt
    assert "restate\nevery row in that reply" in prompt
    assert "Never guess a value you cannot read" in prompt
    # Whole-branch review (Important 4): the bytes ride only the FIRST
    # `complete()` of the turn now (agent/loop.py), so the prompt has to say
    # that out loud -- a model that deferred reading the table until after
    # its first tool call would be looking at nothing.
    assert "only visible to you on your FIRST reply of this turn" in prompt
    # Not baked into the user-editable IDENTITY.md fallback.
    assert "Screenshots of positions" not in DEFAULT_IDENTITY
