from __future__ import annotations

import ast
import math
from decimal import Decimal

VARIABLES = frozenset(
    {"price", "position_weight", "position_qty", "avg_entry_price",
     "pnl_pct", "target_weight"}
)

_ALLOWED_CMPOPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq)


class ConditionError(Exception):
    pass


def parse_condition(text: str) -> ast.Expression:
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"syntax error in condition: {text!r}") from exc
    try:
        _validate(tree.body, top=True)
    except RecursionError as exc:
        raise ConditionError("condition too deeply nested") from exc
    return tree


def _validate(node: ast.AST, top: bool = False) -> None:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for v in node.values:
            _validate(v, top=True)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _validate(node.operand, top=True)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if not isinstance(op, _ALLOWED_CMPOPS):
                raise ConditionError(f"operator not allowed: {ast.dump(op)}")
        for operand in [node.left, *node.comparators]:
            _validate_operand(operand)
    elif top:
        raise ConditionError(
            f"condition must be a comparison or boolean expression: {ast.dump(node)}")
    else:
        raise ConditionError(f"disallowed syntax: {ast.dump(node)}")


def _validate_operand(node: ast.AST) -> None:
    if isinstance(node, ast.Name):
        if node.id not in VARIABLES:
            raise ConditionError(f"unknown variable: {node.id}")
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise ConditionError(f"only numeric literals allowed: {node.value!r}")
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise ConditionError(f"non-finite literal: {node.value!r}")
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        _validate_operand(node.operand)
    else:
        raise ConditionError(f"disallowed operand: {ast.dump(node)}")


def evaluate_condition(text: str, ctx: dict[str, Decimal]) -> bool:
    tree = parse_condition(text)
    try:
        return bool(_eval(tree.body, ctx))
    except RecursionError as exc:
        raise ConditionError("condition too deeply nested") from exc


def _eval(node: ast.AST, ctx: dict[str, Decimal]) -> object:
    if isinstance(node, ast.BoolOp):
        results = (_eval(v, ctx) for v in node.values)
        return any(results) if isinstance(node.op, ast.Or) else all(results)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, ctx)
    if isinstance(node, ast.Compare):
        left = _operand(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators):
            right = _operand(comparator, ctx)
            ok = {
                ast.Lt: left < right, ast.LtE: left <= right,
                ast.Gt: left > right, ast.GtE: left >= right,
                ast.Eq: left == right,
            }[type(op)]
            if not ok:
                return False
            left = right
        return True
    raise ConditionError(f"unexpected node during eval: {ast.dump(node)}")


def caps_position_weight(text: str) -> bool:
    """True when the condition can only be true while position_weight is below
    some MEANINGFUL bound. Used to stop a re-arming buy rule from re-buying
    without limit: an `and` chain needs one capping term, an `or` needs every
    branch capped, and `not` is never treated as a cap.

    Finding 4 (fix pass 2026-09-26): a bound only counts as a cap when it's a
    numeric literal <= 1 (e.g. `0.3`, `1`, or a negated constant like `-5` --
    the condition grammar allows a unary minus on constants) or the name
    `target_weight`. `position_weight < 5`, `< price`, and `< 1.5` must NOT
    count -- each still lets a re-arming buy rule keep buying past 100% of
    equity, which is exactly what this check exists to prevent. A negative
    literal bound is treated as a (trivially satisfied) cap rather than
    special-cased out: it can never actually let the condition go true in
    practice (position_weight can't be negative), so which way it's treated
    doesn't change what the rule can do."""
    return _caps(parse_condition(text).body)


def _is_position_weight(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "position_weight"


def _is_target_weight(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "target_weight"


def _literal_bound(node: ast.AST) -> Decimal | None:
    """The numeric value of `node` if it's a constant, optionally negated by
    a unary minus (the only unary operator the condition grammar allows on a
    constant) -- else None (a variable name, or anything else)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal_bound(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    return None


def _is_meaningful_bound(node: ast.AST) -> bool:
    """See `caps_position_weight`'s docstring: only `target_weight` or a
    numeric literal <= 1 counts as an actual cap."""
    if _is_target_weight(node):
        return True
    value = _literal_bound(node)
    return value is not None and value <= 1


def _caps(node: ast.AST) -> bool:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return any(_caps(v) for v in node.values)
        return all(_caps(v) for v in node.values)
    if isinstance(node, ast.Compare):
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            if (_is_position_weight(left) and isinstance(op, (ast.Lt, ast.LtE))
                    and not _is_position_weight(right) and _is_meaningful_bound(right)):
                return True
            if (_is_position_weight(right) and isinstance(op, (ast.Gt, ast.GtE))
                    and not _is_position_weight(left) and _is_meaningful_bound(left)):
                return True
            left = right
    return False


def _operand(node: ast.AST, ctx: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Name):
        try:
            return ctx[node.id]
        except KeyError as exc:
            raise ConditionError(f"missing context value: {node.id}") from exc
    if isinstance(node, ast.Constant):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_operand(node.operand, ctx)
    raise ConditionError(f"unexpected operand during eval: {ast.dump(node)}")
