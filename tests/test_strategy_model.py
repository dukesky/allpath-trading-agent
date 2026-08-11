from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from allpath_trade.strategy.loader import (
    StrategyValidationError,
    load_strategy,
    parse_strategy_text,
)
from allpath_trade.strategy.model import (
    Authorization,
    PositionPlan,
    Rule,
    RuleState,
    RuleType,
    StrategyBias,
    StrategyDoc,
    StrategyHorizon,
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
    doc = parse_strategy_text("aapl-long", GOOD_YAML)
    assert doc.id == "aapl-long" and doc.position.ticker == "AAPL"


def test_notify_email_defaults_true_when_absent(tmp_path):
    # Backward compatibility: a YAML written before this field existed (and
    # GOOD_YAML above, which has no notify_email key) must still load as
    # notifying -- the toggle is opt-out, not opt-in.
    doc = load_strategy(write(tmp_path, GOOD_YAML))
    assert doc.notify_email is True


def test_notify_email_false_round_trips_through_parse_and_dump(tmp_path):
    text = GOOD_YAML + "\nnotify_email: false\n"
    doc = parse_strategy_text("aapl-long", text)
    assert doc.notify_email is False
    dumped = yaml.safe_dump(doc.model_dump(mode="json"), sort_keys=False,
                            allow_unicode=True)
    reparsed = parse_strategy_text("aapl-long", dumped)
    assert reparsed.notify_email is False


# --- horizon / bias: optional, absent-in-YAML-means-None ------------------

def test_horizon_and_bias_default_to_none_when_absent():
    # GOOD_YAML has no horizon/bias keys -- must come back None, never
    # guessed, so the strategies-page chips simply don't render.
    doc = parse_strategy_text("aapl-long", GOOD_YAML)
    assert doc.horizon is None
    assert doc.bias is None


def test_horizon_and_bias_round_trip_through_parse_and_dump():
    text = GOOD_YAML + "\nhorizon: long\nbias: bullish\n"
    doc = parse_strategy_text("aapl-long", text)
    assert doc.horizon == StrategyHorizon.LONG
    assert doc.bias == StrategyBias.BULLISH
    dumped = yaml.safe_dump(doc.model_dump(mode="json"), sort_keys=False,
                            allow_unicode=True)
    reparsed = parse_strategy_text("aapl-long", dumped)
    assert reparsed.horizon == StrategyHorizon.LONG
    assert reparsed.bias == StrategyBias.BULLISH


def test_invalid_horizon_value_is_rejected():
    text = GOOD_YAML + "\nhorizon: eternal\n"
    with pytest.raises(StrategyValidationError):
        parse_strategy_text("aapl-long", text)


def test_invalid_bias_value_is_rejected():
    text = GOOD_YAML + "\nbias: sideways\n"
    with pytest.raises(StrategyValidationError):
        parse_strategy_text("aapl-long", text)
