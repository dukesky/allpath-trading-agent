from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import yaml
from pydantic import ValidationError

from allpath_trade.strategy.actions import (
    ActionError,
    ActionKind,
    is_option_action,
    parse_action,
)
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


def parse_strategy_text(strategy_id: str, text: str, *, authoring: bool = False) -> StrategyDoc:
    """Parse and validate strategy YAML text.

    Args:
        strategy_id: The strategy identifier
        text: The YAML text to parse
        authoring: Set True only by the surfaces that PROPOSE new/changed
            strategy content -- `agent/action_tools.py`'s `draft_strategy`,
            `agent/reflection_tools.py`'s `propose_strategy_revision`, and
            `strategy/apply.py`'s applier (the only place that ever writes
            a strategy YAML file). It enables two authoring-time-only
            checks that must never block a plain LOAD of an already-saved
            file: (1) an option action (buy_call/buy_put/close_options)
            requires `authorization: auto` + `type: hard` on its rule, and
            (2) a strategy with a buy_call/buy_put action must also carry
            at least one close_options rule (an exit for every entry).

            Every load-only caller -- `load_strategy` (and so
            `StrategyStore.load`/`load_all`), the web status/notify-email
            toggle routes, `StrategyStore.set_authorization`, and every
            call site here that re-parses the CURRENT/base file just to
            diff a version or an authorization/status field against it --
            leaves this False. That matters because a strategy that was
            valid when authored can later fail check (1): the drawdown
            breaker (`risk/breaker.py`) demotes an `auto` strategy to
            `confirm` by flipping one field, with no re-validation. If
            loading enforced check (1), that demoted strategy would raise
            `StrategyValidationError` on every subsequent `load_all` --
            `StrategyStore.load_all` treats that as "skip this file", so
            EVERY rule in it (including a `close_options` stop-loss)
            would silently stop being evaluated, precisely during the
            drawdown the breaker exists to protect against. See
            `sentinel.py`'s `_dispatch_option` for the runtime last line
            of defense that still keeps a demoted strategy's buy rules
            from firing even though loading no longer blocks them.
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

    has_option_entry = False
    has_option_exit = False
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
            if is_option_action(action_spec):
                if action_spec.kind in (ActionKind.BUY_CALL, ActionKind.BUY_PUT):
                    has_option_entry = True
                elif action_spec.kind == ActionKind.CLOSE_OPTIONS:
                    has_option_exit = True
                # Finding 1a: authoring-time only -- see the `authoring`
                # param's docstring above for why a plain load must never
                # enforce this.
                if authoring and not (
                    doc.authorization == Authorization.AUTO and rule.type == RuleType.HARD
                ):
                    errors.append(
                        f"rule {rule.id}: option actions require authorization: auto "
                        "and rule type: hard (v1 limitation)"
                    )
    # Finding 4: authoring-time only, same reasoning as above -- a strategy
    # that proposes an option ENTRY (buy_call/buy_put) without ANY
    # close_options rule anywhere in it has no way to exit that position
    # short of the DTE<=1 expiry safety sweep (sentinel.py), which is a
    # last-resort backstop, not a substitute for a profit-target/stop-loss
    # exit the strategy author is supposed to define. Checked at the
    # document level (not per-rule): one close_options rule can legitimately
    # cover multiple entry rules on the same underlying.
    if authoring and has_option_entry and not has_option_exit:
        errors.append(
            "strategy has a buy_call/buy_put action but no close_options rule "
            "-- every option entry must have a matching exit rule (v1 requirement)"
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
