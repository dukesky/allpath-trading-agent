"""Which reflection revisions may apply with no human (TDSC red-team F1).

Under the auto regime, `Reflector._auto_apply_revisions` approves the run's
own revisions. The applier's freeze only covers `authorization` and `status`,
so before this gate a revision could retarget `position.ticker`, strip a
stop-loss or add an always-true buy, all unattended (red-team S10/S11/S12).

This is an ALLOW-LIST: `auto_apply_holds(old, new)` returns the reasons a
revision must wait for a human, and an empty list only when every change is
one of:

- text: `name`, `thesis`, `horizon`, `bias`, `review`, plus `version`;
- an existing rule whose condition keeps its exact shape (same variables,
  operators and nesting; only numeric literals move) where every moved
  literal tightens the rule:
  - sell rules (exits): the rule may fire MORE often, never less;
  - buy rules: the rule may fire less often, or more often by at most
    `BUY_ADJUST_LIMIT` (10%) of the literal -- a small re-anchoring, not a
    trivially-true condition;
- an exit that sells more, or a buy that spends less (same action kind);
- a buy rule removed; a sell rule re-armed in YAML; a position target or cap
  lowered; email notifications turned on.

Anything else -- a new rule of any kind, a removed or loosened exit, a
retargeted ticker, a raised size, a changed rule type in the riskier
direction, a structural condition change, a field this module does not know
-- is held. Under the human regime nothing here runs: every revision waits
for a human anyway.
"""

from __future__ import annotations

import ast
from decimal import Decimal

from allpath_trade.strategy.actions import ActionError, ActionKind, ActionSpec, parse_action
from allpath_trade.strategy.conditions import ConditionError, parse_condition
from allpath_trade.strategy.model import Rule, RuleState, RuleType, StrategyDoc

# A buy rule's literal may move in the loosening direction by at most this
# fraction of its old value and still count as an adjustment.
BUY_ADJUST_LIMIT = Decimal("0.10")

# Fields whose change never needs a human.
_TEXT_FIELDS = ("name", "thesis", "horizon", "bias", "review", "version")
# Fields this module reasons about itself.
_HANDLED_FIELDS = frozenset({"id", *_TEXT_FIELDS, "status", "authorization",
                             "position", "rules", "notify_email"})

_SELL_KINDS = frozenset({ActionKind.SELL_ALL, ActionKind.SELL_PCT,
                         ActionKind.SELL_VALUE, ActionKind.CLOSE_OPTIONS})
_POSITION_SIZE_FIELDS = ("target_weight", "target_value", "max_weight", "max_value")


def auto_apply_holds(old: StrategyDoc, new: StrategyDoc) -> list[str]:
    """Reasons `new` must be held for a human instead of auto-applied over
    `old`. Empty means the diff stays on the allow-list. Never raises: a
    revision this module cannot read is held, with the reason."""
    reasons: list[str] = []
    try:
        _check_fields(old, new, reasons)
        _check_position(old, new, reasons)
        _check_rules(old, new, reasons)
    except Exception as exc:  # noqa: BLE001 — unclassifiable means held
        reasons.append(f"could not classify the revision ({exc})")
    return list(dict.fromkeys(reasons))


def _check_fields(old: StrategyDoc, new: StrategyDoc, reasons: list[str]) -> None:
    for field in ("authorization", "status"):
        if getattr(old, field) != getattr(new, field):
            reasons.append(f"changes {field}")
    if old.notify_email and not new.notify_email:
        reasons.append("turns off notify_email")
    old_dump, new_dump = old.model_dump(), new.model_dump()
    for field in sorted(set(old_dump) | set(new_dump)):
        if field not in _HANDLED_FIELDS and old_dump.get(field) != new_dump.get(field):
            reasons.append(f"changes {field}")


def _check_position(old: StrategyDoc, new: StrategyDoc, reasons: list[str]) -> None:
    if old.position.ticker != new.position.ticker:
        reasons.append(f"changes position.ticker ({old.position.ticker} -> "
                       f"{new.position.ticker})")
    for field in _POSITION_SIZE_FIELDS:
        before = getattr(old.position, field)
        after = getattr(new.position, field)
        if before == after:
            continue
        is_cap = field.startswith("max_")
        if before is None:
            # Adding a cap tightens; adding a new target switches sizing.
            if not is_cap:
                reasons.append(f"sets position.{field}")
        elif after is None:
            reasons.append(f"removes position.{field}")
        elif after > before:
            reasons.append(f"raises position.{field} ({before} -> {after})")


def _check_rules(old: StrategyDoc, new: StrategyDoc, reasons: list[str]) -> None:
    old_rules = {r.id: r for r in old.rules}
    new_rules = {r.id: r for r in new.rules}
    for rule_id, rule in old_rules.items():
        if rule_id not in new_rules and _is_sell(rule):
            reasons.append(f"removes sell rule {rule_id}")
    for rule_id in new_rules:
        if rule_id not in old_rules:
            reasons.append(f"adds rule {rule_id}")
    for rule_id, before in old_rules.items():
        after = new_rules.get(rule_id)
        if after is not None and before != after:
            _check_rule_change(before, after, reasons)


def _is_sell(rule: Rule) -> bool:
    try:
        return parse_action(rule.action).kind in _SELL_KINDS
    except ActionError:
        return False


def _check_rule_change(before: Rule, after: Rule, reasons: list[str]) -> None:
    old_action = parse_action(before.action)
    new_action = parse_action(after.action)
    sell = old_action.kind in _SELL_KINDS
    side = "sell" if sell else "buy"
    label = f"{side} rule {before.id}"

    if (new_action.kind in _SELL_KINDS) != sell:
        reasons.append(f"changes {before.id} from {side} to "
                       f"{'buy' if sell else 'sell'}")
        return

    # Rule type: hard runs unattended under auto. An exit may become hard
    # (it fires for sure); a buy may become soft (it gets a review).
    if before.type != after.type:
        riskier = (before.type == RuleType.HARD) if sell else (after.type == RuleType.HARD)
        if riskier:
            reasons.append(f"changes {label} type {before.type.value} -> "
                           f"{after.type.value}")

    _check_action(label, sell, old_action, new_action, reasons)
    _check_condition(label, sell, before.condition, after.condition, reasons)
    _check_state(label, sell, before.state, after.state, reasons)
    if _fires_more(before, after) if not sell else _fires_more(after, before):
        reasons.append(f"makes {label} fire more often (rearm)" if not sell
                       else f"makes {label} fire less often (rearm)")


def _check_action(label: str, sell: bool, old: ActionSpec, new: ActionSpec,
                  reasons: list[str]) -> None:
    if old == new:
        return
    if sell:
        if new.kind == ActionKind.SELL_ALL and old.kind in (ActionKind.SELL_PCT,
                                                            ActionKind.SELL_VALUE):
            return  # sells more
        if old.kind == new.kind and old.amount is not None and new.amount is not None:
            if new.amount >= old.amount:
                return
            reasons.append(f"sells less on {label}")
            return
        reasons.append(f"sells less on {label}" if old.kind == ActionKind.SELL_ALL
                       else f"changes the action of {label}")
        return
    if (old.kind == new.kind == ActionKind.BUY_VALUE
            and new.amount is not None and old.amount is not None):
        if new.amount > old.amount:
            reasons.append(f"buys more on {label} (${old.amount} -> ${new.amount})")
        return
    reasons.append(f"changes the action of {label}")


def _check_state(label: str, sell: bool, before: RuleState, after: RuleState,
                 reasons: list[str]) -> None:
    if before == after:
        return
    if sell and after != RuleState.ARMED:
        reasons.append(f"disables {label}")
    elif not sell and after == RuleState.ARMED:
        reasons.append(f"re-arms {label}")


def _fires_more(before: Rule, after: Rule) -> bool:
    """True when `after` may fire more often than `before` by its rearm
    settings alone (one-shot < rearming; shorter rearm or a higher daily cap
    fires more)."""
    if after.rearm is None:
        return False
    if before.rearm is None:
        return True
    return (after.rearm < before.rearm
            or (after.max_fires_per_day or 0) > (before.max_fires_per_day or 0))


# -- conditions --------------------------------------------------------------

def _check_condition(label: str, sell: bool, before: str, after: str,
                     reasons: list[str]) -> None:
    if before == after:
        return
    try:
        old_tree = parse_condition(before).body
        new_tree = parse_condition(after).body
    except ConditionError as exc:
        reasons.append(f"could not read the condition of {label} ({exc})")
        return
    if _shape(old_tree) != _shape(new_tree):
        reasons.append(f"changes the condition structure of {label}")
        return
    for (old_val, grows_when_raised), (new_val, _) in zip(
            _literals(old_tree), _literals(new_tree), strict=True):
        if old_val == new_val:
            continue
        if grows_when_raised is None:
            reasons.append(f"moves an equality in {label}")
            continue
        fires_more = (new_val > old_val) == grows_when_raised
        if sell and not fires_more:
            reasons.append(f"loosens {label} ({before} -> {after})")
        elif not sell and fires_more:
            limit = abs(old_val) * BUY_ADJUST_LIMIT
            if old_val == 0 or abs(new_val - old_val) > limit:
                reasons.append(f"loosens {label} beyond {BUY_ADJUST_LIMIT:.0%} "
                               f"({before} -> {after})")


class _MaskLiterals(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return ast.copy_location(ast.Constant(value=0), node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        if isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
            return ast.copy_location(ast.Constant(value=0), node)
        return self.generic_visit(node)


def _shape(tree: ast.AST) -> str:
    """The condition with every numeric literal (sign included) masked."""
    import copy

    return ast.dump(_MaskLiterals().visit(copy.deepcopy(tree)))


def _literal_value(node: ast.AST) -> Decimal | None:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal_value(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    return None


def _literals(tree: ast.AST, negated: bool = False) -> list[tuple[Decimal, bool | None]]:
    """Every numeric literal in source order, each with whether RAISING it
    makes the condition true more often (True), less often (False), or
    neither (None: an equality). `not` flips the direction; and/or do not
    (the grammar is monotone in each comparison)."""
    out: list[tuple[Decimal, bool | None]] = []
    if isinstance(tree, ast.BoolOp):
        for value in tree.values:
            out.extend(_literals(value, negated))
    elif isinstance(tree, ast.UnaryOp) and isinstance(tree.op, ast.Not):
        out.extend(_literals(tree.operand, not negated))
    elif isinstance(tree, ast.Compare):
        operands = [tree.left, *tree.comparators]
        # Each operand's direction comes from the comparison(s) it sits in.
        # A literal in the middle of a chain (`1 < x < 5` has none, but
        # `x < 5 < y` does) is judged by both sides; conflicting sides mean
        # "unknown", which callers treat like an equality (held).
        for index, node in enumerate(operands):
            value = _literal_value(node)
            if value is None:
                continue
            directions = set()
            if index > 0:  # literal is the RIGHT side of ops[index-1]
                directions.add(_direction(tree.ops[index - 1], literal_on_right=True))
            if index < len(tree.ops):  # literal is the LEFT side of ops[index]
                directions.add(_direction(tree.ops[index], literal_on_right=False))
            direction = directions.pop() if len(directions) == 1 else None
            if direction is not None and negated:
                direction = not direction
            out.append((value, direction))
    return out


def _direction(op: ast.cmpop, *, literal_on_right: bool) -> bool | None:
    """Whether raising the literal makes `a OP b` true more often."""
    if isinstance(op, ast.Eq):
        return None
    less = isinstance(op, (ast.Lt, ast.LtE))
    # x < C: raising C widens. C < x: raising C narrows. Mirror for >.
    return less if literal_on_right else not less
