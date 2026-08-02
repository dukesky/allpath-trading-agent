from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradewind.strategy.loader import StrategyValidationError, load_strategy
from tradewind.strategy.model import (
    Authorization,
    PositionPlan,
    Rule,
    RuleState,
    RuleType,
    StrategyDoc,
    StrategyStatus,
)

GOOD_YAML = """
name: "AAPL long"
status: active
version: 3
authorization: confirm
thesis: |
  Services growth.
position:
  ticker: aapl
  target_weight: 15%
  max_weight: 20%
rules:
  - id: stop-loss
    type: hard
    condition: "price < 185"
    action: "sell all"
  - id: add-on-dip
    type: soft
    condition: "price < 205 and position_weight < target_weight"
    action: "buy $3000"
review:
  cadence: daily
  invalidation: "services growth stalls"
"""


def write(tmp_path: Path, text: str, name: str = "aapl-long.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_good_strategy(tmp_path):
    doc = load_strategy(write(tmp_path, GOOD_YAML))
    assert doc.id == "aapl-long"
    assert doc.position.ticker == "AAPL"
    assert doc.position.target_weight == Decimal("0.15")
    assert doc.position.max_weight == Decimal("0.20")
    assert doc.rules[0].state == RuleState.ARMED
    assert doc.rules[0].type == RuleType.HARD
    assert doc.authorization == Authorization.CONFIRM
    assert doc.status == StrategyStatus.ACTIVE


def test_percent_and_dollar_coercion():
    p = PositionPlan(ticker="MSFT", target_weight="10%", max_value="$9,000")
    assert p.target_weight == Decimal("0.10")
    assert p.max_value == Decimal(9000)
    p2 = PositionPlan(ticker="MSFT", target_value=15000)
    assert p2.target_value == Decimal(15000)


def test_position_requires_a_target():
    with pytest.raises(ValidationError):
        PositionPlan(ticker="MSFT")


def test_duplicate_rule_ids_rejected():
    rules = [
        Rule(id="r1", type=RuleType.HARD, condition="price < 1", action="sell all"),
        Rule(id="r1", type=RuleType.SOFT, condition="price > 2", action="buy $100"),
    ]
    with pytest.raises(ValidationError):
        StrategyDoc(id="x", name="x", position=PositionPlan(ticker="A", target_weight="5%"),
                    rules=rules)


def test_load_bad_yaml_collects_errors(tmp_path):
    with pytest.raises(StrategyValidationError) as ei:
        load_strategy(write(tmp_path, "name: [unclosed", name="bad.yaml"))
    assert ei.value.errors


def test_load_missing_position_reports_error(tmp_path):
    with pytest.raises(StrategyValidationError) as ei:
        load_strategy(write(tmp_path, "name: x\nstatus: draft\n", name="nopos.yaml"))
    assert any("position" in e for e in ei.value.errors)


def test_load_rejects_bad_condition_and_action(tmp_path):
    bad = GOOD_YAML.replace('"price < 185"', '"__import__(\'os\')"').replace(
        '"buy $3000"', '"hold everything"')
    with pytest.raises(StrategyValidationError) as ei:
        load_strategy(write(tmp_path, bad, name="badrules.yaml"))
    joined = " ".join(ei.value.errors)
    assert "stop-loss" in joined and "add-on-dip" in joined


def test_load_action_typo_collects_error_not_crash(tmp_path):
    bad = GOOD_YAML.replace('"buy $3000"', '"buy $."')
    with pytest.raises(StrategyValidationError) as ei:
        load_strategy(write(tmp_path, bad, name="typo.yaml"))
    assert any("add-on-dip" in e for e in ei.value.errors)


def test_parse_strategy_text_matches_load(tmp_path):
    from tradewind.strategy.loader import parse_strategy_text

    doc = parse_strategy_text("aapl-long", GOOD_YAML)
    assert doc.id == "aapl-long" and doc.position.ticker == "AAPL"
