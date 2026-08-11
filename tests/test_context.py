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
