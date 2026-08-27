from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import yaml
from pydantic import ValidationError

from allpath_trade.strategy.actions import ActionError, is_option_action, parse_action
from allpath_trade.strategy.conditions import ConditionError, parse_condition
from allpath_trade.strategy.model import Authorization, RuleType, StrategyDoc

_VALID_STRATEGY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def is_valid_strategy_id(strategy_id: str) -> bool:
    return bool(_VALID_STRATEGY_ID.match(strategy_id))


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` without ever exposing a truncated file to a
    concurrent reader. The sentinel scheduler polls strategy YAML from the
    same process that serves web writes (draft_strategy, the notify-email
    toggle), so a plain `write_text` -- which truncates the file before the
    new bytes land -- has a window where a mid-write read sees a
    valid-but-empty prefix. `rules` then defaults to `[]`, so that pass
    silently evaluates zero rules (including a hard stop-loss) with no error
    raised. Writing to a temp file in the same directory and `os.replace`ing
    it over the target is atomic on POSIX: readers see either the old
    content or the new content, never a partial write."""
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


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
            action_spec = parse_action(rule.action)
        except ActionError as exc:
            errors.append(f"rule {rule.id}: {exc}")
        else:
            if is_option_action(action_spec) and not (
                doc.authorization == Authorization.AUTO and rule.type == RuleType.HARD
            ):
                errors.append(
                    f"rule {rule.id}: option actions require authorization: auto "
                    "and rule type: hard (v1 limitation)"
                )
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
