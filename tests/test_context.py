from tests.test_sentinel import FakeBroker
from tradewind.agent.context import DEFAULT_IDENTITY, build_system_prompt, load_identity
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal
from tradewind.store.reviews import ReviewQueue
from tradewind.strategy.store import StrategyStore

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
