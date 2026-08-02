from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from allpath_trade.strategy.actions import ActionError, parse_action
from allpath_trade.strategy.conditions import ConditionError, parse_condition
from allpath_trade.strategy.model import StrategyDoc

_VALID_STRATEGY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def is_valid_strategy_id(strategy_id: str) -> bool:
    return bool(_VALID_STRATEGY_ID.match(strategy_id))


class StrategyValidationError(Exception):
    def __init__(self, strategy_id: str, errors: list[str]) -> None:
        self.strategy_id = strategy_id
        self.errors = errors
        super().__init__(f"invalid strategy '{strategy_id}': " + "; ".join(errors))


def parse_strategy_text(strategy_id: str, text: str) -> StrategyDoc:
    """Parse and validate strategy YAML text.

    Args:
        strategy_id: The strategy identifier
        text: The YAML text to parse

    Returns:
        Parsed and validated StrategyDoc

    Raises:
        StrategyValidationError: If parsing or validation fails
    """
    errors: list[str] = []
    try:
        raw = yaml.safe_load(text)
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


def load_strategy(path: Path) -> StrategyDoc:
    """Load a strategy from a YAML file.

    Args:
        path: Path to the YAML file

    Returns:
        Parsed and validated StrategyDoc

    Raises:
        StrategyValidationError: If parsing or validation fails
        FileNotFoundError: If the file doesn't exist
    """
    return parse_strategy_text(path.stem, path.read_text())
