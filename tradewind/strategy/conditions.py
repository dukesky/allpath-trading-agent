from __future__ import annotations

import ast
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
    _validate(tree.body, top=True)
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
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        _validate_operand(node.operand)
    else:
        raise ConditionError(f"disallowed operand: {ast.dump(node)}")


def evaluate_condition(text: str, ctx: dict[str, Decimal]) -> bool:
    tree = parse_condition(text)
    return bool(_eval(tree.body, ctx))


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
