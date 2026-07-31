from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from tradewind.strategy.actions import ActionError, parse_action
from tradewind.strategy.conditions import ConditionError, parse_condition
from tradewind.strategy.model import StrategyDoc


class StrategyValidationError(Exception):
    def __init__(self, strategy_id: str, errors: list[str]) -> None:
        self.strategy_id = strategy_id
        self.errors = errors
        super().__init__(f"invalid strategy '{strategy_id}': " + "; ".join(errors))


def load_strategy(path: Path) -> StrategyDoc:
    strategy_id = path.stem
    errors: list[str] = []
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise StrategyValidationError(strategy_id, [f"YAML parse error: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise StrategyValidationError(strategy_id, ["document is not a mapping"])
    raw["id"] = strategy_id
    try:
        doc = StrategyDoc.model_validate(raw)
    except ValidationError as exc:
        for e in exc.errors():
            loc = ".".join(str(p) for p in e["loc"])
            errors.append(f"{loc}: {e['msg']}")
        raise StrategyValidationError(strategy_id, errors) from exc

    for rule in doc.rules:
        try:
            parse_condition(rule.condition)
        except ConditionError as exc:
            errors.append(f"rule {rule.id}: {exc}")
        try:
            parse_action(rule.action)
        except ActionError as exc:
            errors.append(f"rule {rule.id}: {exc}")
    if errors:
        raise StrategyValidationError(strategy_id, errors)
    return doc
