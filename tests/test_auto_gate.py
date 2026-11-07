"""TDSC red-team F1: under the auto regime, a reflection revision applies with
no human only when its diff stays on an allow-list -- thesis/notes text, and
tightening (or a small adjustment of) the thresholds of existing rules.
Everything else is held for a human, with reasons the card can show."""

from __future__ import annotations

import yaml

from allpath_trade.strategy.auto_gate import auto_apply_holds
from allpath_trade.strategy.loader import parse_strategy_text

BASE = {
    "name": "AAPL dip",
    "status": "active",
    "version": 3,
    "authorization": "auto",
    "thesis": "Services margin expansion.",
    "position": {"ticker": "AAPL", "target_weight": "15%", "max_weight": "20%"},
    "rules": [
        {"id": "stop-loss", "type": "hard", "condition": "price < 150",
         "action": "sell all"},
        {"id": "trail", "type": "hard", "condition": "pnl_pct < -0.08",
         "action": "sell 50%"},
        {"id": "take-profit", "type": "soft", "condition": "price > 250",
         "action": "sell 25%"},
        {"id": "dip-buy", "type": "hard",
         "condition": "price < 180 and position_weight < 0.15",
         "action": "buy $3000"},
    ],
}


def doc(raw):
    return parse_strategy_text("aapl", yaml.safe_dump(raw))


def revise(**changes):
    raw = yaml.safe_load(yaml.safe_dump(BASE))
    raw["version"] = 4
    for key, value in changes.items():
        raw[key] = value
    return raw


def rules_with(rule_id, **fields):
    rules = yaml.safe_load(yaml.safe_dump(BASE["rules"]))
    for r in rules:
        if r["id"] == rule_id:
            r.update(fields)
    return rules


def holds(new_raw):
    return auto_apply_holds(doc(BASE), doc(new_raw))


# -- allowed -----------------------------------------------------------------

def test_thesis_and_name_edits_auto_apply():
    assert holds(revise(thesis="Updated after earnings.", name="AAPL dip v2")) == []


def test_raising_a_long_stop_is_tightening():
    assert holds(revise(rules=rules_with("stop-loss", condition="price < 160"))) == []


def test_tightening_a_trailing_stop_is_allowed():
    assert holds(revise(rules=rules_with("trail", condition="pnl_pct < -0.05"))) == []


def test_lowering_a_take_profit_sells_sooner_and_is_allowed():
    assert holds(revise(rules=rules_with("take-profit", condition="price > 240"))) == []


def test_selling_more_on_a_stop_is_allowed():
    assert holds(revise(rules=rules_with("trail", action="sell all"))) == []


def test_tightening_a_buy_threshold_is_allowed():
    assert holds(revise(rules=rules_with(
        "dip-buy", condition="price < 170 and position_weight < 0.15"))) == []


def test_small_buy_threshold_adjustment_is_allowed():
    # Moving a dip-buy level up with the price (<= 10%) is an adjustment.
    assert holds(revise(rules=rules_with(
        "dip-buy", condition="price < 195 and position_weight < 0.15"))) == []


def test_smaller_buy_amount_is_allowed():
    assert holds(revise(rules=rules_with("dip-buy", action="buy $2000"))) == []


def test_removing_a_buy_rule_is_allowed():
    rules = [r for r in BASE["rules"] if r["id"] != "dip-buy"]
    assert holds(revise(rules=rules)) == []


def test_lowering_target_weight_is_allowed():
    assert holds(revise(position={"ticker": "AAPL", "target_weight": "10%",
                                  "max_weight": "20%"})) == []


# -- (a) ticker --------------------------------------------------------------

def test_changing_position_ticker_is_held():
    reasons = holds(revise(position={"ticker": "GME", "target_weight": "15%",
                                     "max_weight": "20%"}))
    assert any("changes position.ticker (AAPL -> GME)" in r for r in reasons)


# -- (b) exits ---------------------------------------------------------------

def test_removing_a_stop_loss_is_held():
    rules = [r for r in BASE["rules"] if r["id"] != "stop-loss"]
    assert "removes sell rule stop-loss" in holds(revise(rules=rules))


def test_lowering_a_long_stop_is_held():
    reasons = holds(revise(rules=rules_with("stop-loss", condition="price < 140")))
    assert any("loosens sell rule stop-loss" in r for r in reasons)


def test_widening_a_trailing_stop_is_held():
    reasons = holds(revise(rules=rules_with("trail", condition="pnl_pct < -0.15")))
    assert any("loosens sell rule trail" in r for r in reasons)


def test_hard_to_soft_on_a_sell_rule_is_held():
    reasons = holds(revise(rules=rules_with("stop-loss", type="soft")))
    assert any("stop-loss" in r and "hard -> soft" in r for r in reasons)


def test_adding_a_condition_term_to_a_stop_is_held():
    reasons = holds(revise(rules=rules_with(
        "stop-loss", condition="price < 150 and pnl_pct < -0.2")))
    assert any("changes the condition structure of sell rule stop-loss" in r
               for r in reasons)


def test_selling_less_on_a_stop_is_held():
    reasons = holds(revise(rules=rules_with("stop-loss", action="sell 50%")))
    assert any("sells less on sell rule stop-loss" in r for r in reasons)


def test_disabling_a_stop_in_yaml_is_held():
    reasons = holds(revise(rules=rules_with("stop-loss", state="disabled")))
    assert any("disables sell rule stop-loss" in r for r in reasons)


# -- (c) buys ----------------------------------------------------------------

def test_adding_a_hard_buy_rule_is_held():
    rules = [*BASE["rules"], {"id": "always-buy", "type": "hard",
                              "condition": "price > 0", "action": "buy $4900"}]
    assert "adds rule always-buy" in holds(revise(rules=rules))


def test_making_a_buy_condition_effectively_unconditional_is_held():
    reasons = holds(revise(rules=rules_with(
        "dip-buy", condition="price < 100000 and position_weight < 0.15")))
    assert any("loosens buy rule dip-buy" in r for r in reasons)


def test_soft_to_hard_on_a_buy_rule_is_held():
    rules = rules_with("dip-buy", type="soft")
    base = dict(BASE, rules=rules)
    new = dict(base, version=4, rules=rules_with("dip-buy", type="hard"))
    assert any("soft -> hard" in r for r in auto_apply_holds(doc(base), doc(new)))


def test_bigger_buy_amount_is_held():
    reasons = holds(revise(rules=rules_with("dip-buy", action="buy $4900")))
    assert any("buys more on buy rule dip-buy" in r for r in reasons)


def test_turning_a_sell_rule_into_a_buy_is_held():
    reasons = holds(revise(rules=rules_with("take-profit", action="buy $3000")))
    assert any("take-profit" in r for r in reasons)


def test_adding_rearm_to_a_buy_rule_is_held():
    reasons = holds(revise(rules=rules_with("dip-buy", rearm="60m")))
    assert any("fire more often" in r for r in reasons)


# -- everything else ---------------------------------------------------------

def test_raising_target_weight_is_held():
    reasons = holds(revise(position={"ticker": "AAPL", "target_weight": "25%",
                                     "max_weight": "20%"}))
    assert any("raises position.target_weight" in r for r in reasons)


def test_removing_a_max_weight_cap_is_held():
    reasons = holds(revise(position={"ticker": "AAPL", "target_weight": "15%"}))
    assert any("position.max_weight" in r for r in reasons)


def test_turning_off_email_notifications_is_held():
    assert any("notify_email" in r for r in holds(revise(notify_email=False)))


def test_authorization_change_is_held_as_defence_in_depth():
    assert any("authorization" in r for r in holds(revise(authorization="confirm")))


def test_reasons_are_deduplicated_and_stable():
    rules = [r for r in BASE["rules"] if r["id"] not in ("stop-loss", "trail")]
    assert holds(revise(rules=rules)) == ["removes sell rule stop-loss",
                                          "removes sell rule trail"]
