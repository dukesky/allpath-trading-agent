# Phase 2: Strategy Engine + Sentinel Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn strategy YAML documents into automated monitoring and execution: deterministic rule evaluation on a schedule, one-shot triggers dispatched per the (rule type × authorization) matrix — hard/auto executes via the Phase 1 `Executor`, everything else lands in a UI-ready pending-review queue with email notification.

**Architecture:** Strategies live as `strategies/*.yaml` (human-readable current version) with full-text version snapshots and runtime rule states in SQLite. A whitelist-AST condition evaluator and a tiny action grammar convert triggers into `OrderIntent`s. The `Sentinel` orchestrates one check pass; APScheduler drives it hourly during US market hours. `ReviewQueue` is a service API (CLI today, Web UI in Phase 5, agent in Phase 3 — same API).

**Tech Stack:** Python ≥3.11, pydantic v2, PyYAML, stdlib `ast` (whitelist evaluator), APScheduler, smtplib (email), zoneinfo, existing Phase 1 modules (`Executor`, `RiskGate`, `TradeJournal`, `AlpacaBroker`, `YFinanceSource`).

## Global Constraints

- Money is `Decimal`, never float. Weights are fractions (`0.15` = 15%); `pnl_pct` is a percent number (`20` = +20%).
- Condition evaluation uses `ast.parse` + node whitelist — never `eval()`. Unknown nodes/variables are rejected at load time.
- Condition vocabulary v1 (exactly): `price`, `position_weight`, `position_qty`, `avg_entry_price`, `pnl_pct`, `target_weight`. Operators: `< > <= >= ==`, `and/or/not`, parentheses, numeric literals (incl. negative).
- Action grammar v1 (exactly): `sell all` | `sell N%` | `sell $N` | `buy $N` | `buy to target_weight`.
- Trigger semantics: one-shot. A fired rule's state goes `armed → triggered` and is persisted BEFORE any execution attempt (never double-execute). Re-arm is explicit (`tradewind rearm`).
- Dispatch matrix: auth `auto` → hard executes via `Executor.execute`, soft enqueues; `confirm` → both enqueue; `notify` → notify only. Every trigger notifies.
- Rule runtime state lives in SQLite (`rule_states`), never written back into the user's YAML.
- The sentinel never crashes on one bad strategy: per-strategy errors are collected and reported, the loop continues.
- Email is notification-only (no action links). When SMTP is not configured, notifications degrade to console/log — never an error.
- Sentinel interval is a parameter: `Settings.sentinel_interval_minutes: int = 60`.
- All new schema goes through `tradewind/store/db.py` `SCHEMA` (idempotent `CREATE TABLE IF NOT EXISTS`).
- Run everything with `uv run`; commit after every task; commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Harden OrderIntent ticker validation (Phase 3 prerequisite from final review)

**Files:**
- Modify: `tradewind/broker/base.py` (the `_upper` validator)
- Test: `tests/test_broker_base.py`

**Interfaces:**
- Produces: `OrderIntent(ticker="  ")` and `ticker=""` now raise `ValidationError`. No other behavior changes.

- [ ] **Step 1: Write the failing test** — append to `tests/test_broker_base.py`:

```python
def test_intent_rejects_empty_or_whitespace_ticker():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="", side=OrderSide.BUY, notional=Decimal("100"), reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(ticker="   ", side=OrderSide.BUY, notional=Decimal("100"), reason="x")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_broker_base.py::test_intent_rejects_empty_or_whitespace_ticker -v`
Expected: FAIL (no exception raised)

- [ ] **Step 3: Implement** — in `tradewind/broker/base.py`, change the `_upper` validator to:

```python
    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker must be non-empty")
        return v
```

- [ ] **Step 4: Run full suite** — `uv run pytest -v` → all pass.
- [ ] **Step 5: Commit** — `fix: reject empty/whitespace ticker in OrderIntent`

---

### Task 2: Strategy domain models + YAML loading

**Files:**
- Create: `tradewind/strategy/__init__.py`, `tradewind/strategy/model.py`, `tradewind/strategy/loader.py`
- Test: `tests/test_strategy_model.py`

**Interfaces (produced, used by every later task):**
- Enums (lowercase string values): `RuleType` (`HARD="hard"`, `SOFT="soft"`), `RuleState` (`ARMED="armed"`, `TRIGGERED="triggered"`, `DISABLED="disabled"`), `Authorization` (`NOTIFY="notify"`, `CONFIRM="confirm"`, `AUTO="auto"`), `StrategyStatus` (`DRAFT="draft"`, `ACTIVE="active"`, `PAUSED="paused"`, `ARCHIVED="archived"`).
- `Rule(id: str, type: RuleType, condition: str, action: str, state: RuleState = ARMED)`.
- `PositionPlan(ticker: str, target_weight: Decimal | None = None, target_value: Decimal | None = None, max_weight: Decimal | None = None, max_value: Decimal | None = None)` — ticker uppercased/non-empty; **at least one** of target_weight/target_value required; percent strings (`"15%"`) accepted for weight fields and converted to fractions (`Decimal("0.15")`); dollar strings (`"$15000"`) accepted for value fields.
- `ReviewPolicy(cadence: str = "daily", invalidation: str = "")`.
- `StrategyDoc(id: str, name: str, status: StrategyStatus = DRAFT, version: int = 1, authorization: Authorization = CONFIRM, thesis: str = "", position: PositionPlan, rules: list[Rule] = [], review: ReviewPolicy = ReviewPolicy())` — validator: rule ids unique.
- `loader.load_strategy(path: Path) -> StrategyDoc` — YAML → model; `id` = filename stem; raises `StrategyValidationError(errors: list[str])` listing ALL problems (bad YAML, model errors, unparseable condition/action via Task 3/4 parsers — loader integrates them in Task 4's step).
- NOTE for this task: loader validates model shape only; condition/action parser integration is added in Task 4 Step 5 (after parsers exist).

- [ ] **Step 1: Write the failing test**

`tests/test_strategy_model.py`:
```python
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradewind.strategy.loader import StrategyValidationError, load_strategy
from tradewind.strategy.model import (
    Authorization, PositionPlan, Rule, RuleState, RuleType, StrategyDoc, StrategyStatus,
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
    assert p.max_value == Decimal("9000")
    p2 = PositionPlan(ticker="MSFT", target_value=15000)
    assert p2.target_value == Decimal("15000")


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
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_strategy_model.py -v` → FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement**

`tradewind/strategy/__init__.py`:
```python
from tradewind.strategy.model import (
    Authorization, PositionPlan, ReviewPolicy, Rule, RuleState, RuleType,
    StrategyDoc, StrategyStatus,
)

__all__ = [
    "Authorization", "PositionPlan", "ReviewPolicy", "Rule", "RuleState",
    "RuleType", "StrategyDoc", "StrategyStatus",
]
```

`tradewind/strategy/model.py`:
```python
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum

from pydantic import BaseModel, field_validator, model_validator


class RuleType(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class RuleState(str, Enum):
    ARMED = "armed"
    TRIGGERED = "triggered"
    DISABLED = "disabled"


class Authorization(str, Enum):
    NOTIFY = "notify"
    CONFIRM = "confirm"
    AUTO = "auto"


class StrategyStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


def _to_decimal(raw: object, *, percent: bool) -> Decimal:
    """Accept 0.15 / '0.15' / '15%' (percent fields) / '$9,000' (value fields)."""
    if isinstance(raw, Decimal):
        return raw
    text = str(raw).strip().replace(",", "").replace("$", "")
    try:
        if text.endswith("%"):
            if not percent:
                raise ValueError(f"percent not allowed here: {raw!r}")
            return Decimal(text[:-1]) / Decimal("100")
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {raw!r}") from exc


class Rule(BaseModel):
    id: str
    type: RuleType
    condition: str
    action: str
    state: RuleState = RuleState.ARMED


class PositionPlan(BaseModel):
    ticker: str
    target_weight: Decimal | None = None
    target_value: Decimal | None = None
    max_weight: Decimal | None = None
    max_value: Decimal | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("ticker must be non-empty")
        return v

    @field_validator("target_weight", "max_weight", mode="before")
    @classmethod
    def _pct(cls, v: object) -> object:
        return None if v is None else _to_decimal(v, percent=True)

    @field_validator("target_value", "max_value", mode="before")
    @classmethod
    def _val(cls, v: object) -> object:
        return None if v is None else _to_decimal(v, percent=False)

    @model_validator(mode="after")
    def _has_target(self) -> "PositionPlan":
        if self.target_weight is None and self.target_value is None:
            raise ValueError("position requires target_weight or target_value")
        return self


class ReviewPolicy(BaseModel):
    cadence: str = "daily"
    invalidation: str = ""


class StrategyDoc(BaseModel):
    id: str
    name: str
    status: StrategyStatus = StrategyStatus.DRAFT
    version: int = 1
    authorization: Authorization = Authorization.CONFIRM
    thesis: str = ""
    position: PositionPlan
    rules: list[Rule] = []
    review: ReviewPolicy = ReviewPolicy()

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> "StrategyDoc":
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self
```

`tradewind/strategy/loader.py`:
```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

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
    return doc
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_strategy_model.py -v` → 6 PASSED; full suite green.
- [ ] **Step 5: Commit** — `feat: strategy domain models and YAML loader`

---

### Task 3: Condition evaluator (whitelist AST)

**Files:**
- Create: `tradewind/strategy/conditions.py`
- Test: `tests/test_conditions.py`

**Interfaces:**
- `VARIABLES: frozenset[str]` = {"price", "position_weight", "position_qty", "avg_entry_price", "pnl_pct", "target_weight"}.
- `ConditionError(Exception)` — raised on parse/validation failure with a message naming the offending part.
- `parse_condition(text: str) -> ast.Expression` — parses and validates against the whitelist; raises `ConditionError`.
- `evaluate_condition(text: str, ctx: dict[str, Decimal]) -> bool` — parses (or re-validates) then evaluates recursively; numeric literals become `Decimal(str(literal))`; missing ctx key raises `ConditionError`.

- [ ] **Step 1: Write the failing test**

`tests/test_conditions.py`:
```python
from decimal import Decimal

import pytest

from tradewind.strategy.conditions import (
    ConditionError, evaluate_condition, parse_condition,
)

CTX = {
    "price": Decimal("200"),
    "position_weight": Decimal("0.10"),
    "position_qty": Decimal("5"),
    "avg_entry_price": Decimal("180"),
    "pnl_pct": Decimal("11.1"),
    "target_weight": Decimal("0.15"),
}


@pytest.mark.parametrize("expr,expected", [
    ("price < 185", False),
    ("price >= 200", True),
    ("price == 200", True),
    ("pnl_pct > 10 and position_weight < target_weight", True),
    ("pnl_pct > 20 or price < 250", True),
    ("not (price > 300)", True),
    ("price > -5", True),
    ("position_qty <= 5 and (price < 100 or pnl_pct > 5)", True),
])
def test_evaluate(expr, expected):
    assert evaluate_condition(expr, CTX) is expected


@pytest.mark.parametrize("bad", [
    "__import__('os')",          # call
    "price + 1 < 2",             # arithmetic not in v1
    "foo < 1",                   # unknown variable
    "price < '185'",             # string literal
    "[1,2]",                     # list
    "price",                     # not boolean
    "lambda: 1",
    "price < 1; price > 2",
])
def test_rejects_bad_expressions(bad):
    with pytest.raises(ConditionError):
        parse_condition(bad)


def test_missing_context_key_raises():
    with pytest.raises(ConditionError):
        evaluate_condition("price < 1", {})


def test_decimal_precision():
    assert evaluate_condition("price == 0.1", {"price": Decimal("0.1")}) is True
```

- [ ] **Step 2: Run to verify it fails** — FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement**

`tradewind/strategy/conditions.py`:
```python
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
```

Note: dict lookup in `_eval`'s Compare branch evaluates all entries eagerly — implement exactly as shown (values are plain Decimals; there is no short-circuit hazard).

- [ ] **Step 4: Run** — `uv run pytest tests/test_conditions.py -v` → all pass; full suite green.
- [ ] **Step 5: Commit** — `feat: whitelist-AST condition evaluator`

---

### Task 4: Action parser + OrderIntent conversion + loader integration

**Files:**
- Create: `tradewind/strategy/actions.py`
- Modify: `tradewind/strategy/loader.py` (validate conditions/actions at load)
- Test: `tests/test_actions.py`, extend `tests/test_strategy_model.py`

**Interfaces:**
- `ActionKind` enum: `SELL_PCT`, `SELL_ALL`, `SELL_VALUE`, `BUY_VALUE`, `BUY_TO_TARGET` (lowercase values `sell_pct` etc.).
- `ActionSpec(kind: ActionKind, amount: Decimal | None)` — amount: percent number for SELL_PCT (50 = 50%), dollars for *_VALUE, None otherwise.
- `ActionError(Exception)`.
- `parse_action(text: str) -> ActionSpec` — grammar exactly: `sell all` | `sell N%` | `sell $N` | `buy $N` | `buy to target_weight` (case-insensitive, commas allowed in numbers). Raises `ActionError` otherwise.
- `to_order_intent(spec, *, strategy: StrategyDoc, rule_id: str, price: Decimal, position: Position | None, equity: Decimal, reason: str) -> OrderIntent | None` — returns None when there is nothing to do (sell with no position; buy-to-target already at/above target). Sell qty quantized to 4 decimal places; buy-to-target notional quantized to 2.
- `loader.load_strategy` now ALSO validates every rule's condition (`parse_condition`) and action (`parse_action`), collecting all errors into `StrategyValidationError`.

- [ ] **Step 1: Write the failing test**

`tests/test_actions.py`:
```python
from decimal import Decimal

import pytest

from tradewind.broker.base import OrderSide, Position
from tradewind.strategy.actions import (
    ActionError, ActionKind, parse_action, to_order_intent,
)
from tradewind.strategy.model import PositionPlan, StrategyDoc

STRAT = StrategyDoc(id="s", name="s",
                    position=PositionPlan(ticker="AAPL", target_weight="15%"))
POS = Position(ticker="AAPL", qty=Decimal("10"), avg_entry_price=Decimal("180"),
               market_value=Decimal("2000"), unrealized_pl=Decimal("200"))


@pytest.mark.parametrize("text,kind,amount", [
    ("sell all", ActionKind.SELL_ALL, None),
    ("Sell 50%", ActionKind.SELL_PCT, Decimal("50")),
    ("sell $5,000", ActionKind.SELL_VALUE, Decimal("5000")),
    ("buy $3000", ActionKind.BUY_VALUE, Decimal("3000")),
    ("buy to target_weight", ActionKind.BUY_TO_TARGET, None),
])
def test_parse(text, kind, amount):
    spec = parse_action(text)
    assert spec.kind == kind and spec.amount == amount


@pytest.mark.parametrize("bad", ["sell", "buy 50%", "sell -10%", "hold", "buy $0", "sell 0%"])
def test_parse_rejects(bad):
    with pytest.raises(ActionError):
        parse_action(bad)


def kw(**over):
    base = dict(strategy=STRAT, rule_id="r", price=Decimal("200"),
                position=POS, equity=Decimal("10000"), reason="t")
    base.update(over)
    return base


def test_sell_all_uses_position_qty():
    intent = to_order_intent(parse_action("sell all"), **kw())
    assert intent.side == OrderSide.SELL and intent.qty == Decimal("10")
    assert intent.strategy_id == "s"


def test_sell_pct_quantizes_qty():
    intent = to_order_intent(parse_action("sell 33%"), **kw())
    assert intent.qty == Decimal("3.3000")


def test_sell_with_no_position_returns_none():
    assert to_order_intent(parse_action("sell all"), **kw(position=None)) is None


def test_sell_value_passes_notional():
    intent = to_order_intent(parse_action("sell $1500"), **kw())
    assert intent.notional == Decimal("1500")


def test_buy_value():
    intent = to_order_intent(parse_action("buy $3000"), **kw())
    assert intent.side == OrderSide.BUY and intent.notional == Decimal("3000")


def test_buy_to_target_computes_gap():
    # target 15% of 10000 = 1500; fresh position value = 10 * 200 = 2000 -> at/above target
    assert to_order_intent(parse_action("buy to target_weight"), **kw()) is None
    # with smaller position: 10*100=1000 < 1500 -> buy 500
    intent = to_order_intent(parse_action("buy to target_weight"), **kw(price=Decimal("100")))
    assert intent.notional == Decimal("500.00")


def test_buy_to_target_with_value_mode():
    strat = StrategyDoc(id="v", name="v",
                        position=PositionPlan(ticker="AAPL", target_value="$5000"))
    intent = to_order_intent(parse_action("buy to target_weight"),
                             **kw(strategy=strat))
    assert intent.notional == Decimal("3000.00")  # 5000 - 10*200
```

Extend `tests/test_strategy_model.py`:
```python
def test_load_rejects_bad_condition_and_action(tmp_path):
    bad = GOOD_YAML.replace('"price < 185"', '"__import__(\'os\')"').replace(
        '"buy $3000"', '"hold everything"')
    with pytest.raises(StrategyValidationError) as ei:
        load_strategy(write(tmp_path, bad, name="badrules.yaml"))
    joined = " ".join(ei.value.errors)
    assert "stop-loss" in joined and "add-on-dip" in joined
```

- [ ] **Step 2: Run to verify failures** — new tests FAIL.

- [ ] **Step 3: Implement**

`tradewind/strategy/actions.py`:
```python
from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from tradewind.broker.base import OrderIntent, OrderSide, Position
from tradewind.strategy.model import StrategyDoc


class ActionKind(str, Enum):
    SELL_PCT = "sell_pct"
    SELL_ALL = "sell_all"
    SELL_VALUE = "sell_value"
    BUY_VALUE = "buy_value"
    BUY_TO_TARGET = "buy_to_target"


class ActionSpec(BaseModel):
    kind: ActionKind
    amount: Decimal | None = None


class ActionError(Exception):
    pass


_PATTERNS: list[tuple[re.Pattern[str], ActionKind]] = [
    (re.compile(r"^sell\s+all$", re.I), ActionKind.SELL_ALL),
    (re.compile(r"^sell\s+(?P<num>[\d,.]+)%$", re.I), ActionKind.SELL_PCT),
    (re.compile(r"^sell\s+\$(?P<num>[\d,.]+)$", re.I), ActionKind.SELL_VALUE),
    (re.compile(r"^buy\s+\$(?P<num>[\d,.]+)$", re.I), ActionKind.BUY_VALUE),
    (re.compile(r"^buy\s+to\s+target_weight$", re.I), ActionKind.BUY_TO_TARGET),
]


def parse_action(text: str) -> ActionSpec:
    stripped = text.strip()
    for pattern, kind in _PATTERNS:
        m = pattern.match(stripped)
        if not m:
            continue
        amount: Decimal | None = None
        if "num" in m.groupdict() and m.group("num") is not None:
            amount = Decimal(m.group("num").replace(",", ""))
            if amount <= 0:
                raise ActionError(f"amount must be positive: {text!r}")
            if kind == ActionKind.SELL_PCT and amount > 100:
                raise ActionError(f"sell percent > 100: {text!r}")
        return ActionSpec(kind=kind, amount=amount)
    raise ActionError(f"unrecognized action: {text!r}")


def to_order_intent(spec: ActionSpec, *, strategy: StrategyDoc, rule_id: str,
                    price: Decimal, position: Position | None, equity: Decimal,
                    reason: str) -> OrderIntent | None:
    ticker = strategy.position.ticker
    held_qty = position.qty if position else Decimal("0")

    if spec.kind in (ActionKind.SELL_ALL, ActionKind.SELL_PCT, ActionKind.SELL_VALUE):
        if held_qty <= 0:
            return None
        if spec.kind == ActionKind.SELL_ALL:
            return OrderIntent(ticker=ticker, side=OrderSide.SELL, qty=held_qty,
                               reason=reason, strategy_id=strategy.id)
        if spec.kind == ActionKind.SELL_PCT:
            qty = (held_qty * spec.amount / Decimal("100")).quantize(Decimal("0.0001"))
            if qty <= 0:
                return None
            return OrderIntent(ticker=ticker, side=OrderSide.SELL, qty=qty,
                               reason=reason, strategy_id=strategy.id)
        return OrderIntent(ticker=ticker, side=OrderSide.SELL, notional=spec.amount,
                           reason=reason, strategy_id=strategy.id)

    if spec.kind == ActionKind.BUY_VALUE:
        return OrderIntent(ticker=ticker, side=OrderSide.BUY, notional=spec.amount,
                           reason=reason, strategy_id=strategy.id)

    # BUY_TO_TARGET
    plan = strategy.position
    target_value = (plan.target_value if plan.target_value is not None
                    else plan.target_weight * equity)
    current_value = held_qty * price
    gap = (target_value - current_value).quantize(Decimal("0.01"))
    if gap <= 0:
        return None
    return OrderIntent(ticker=ticker, side=OrderSide.BUY, notional=gap,
                       reason=reason, strategy_id=strategy.id)
```

Modify `tradewind/strategy/loader.py` — after successful `StrategyDoc.model_validate`, add rule validation before `return doc`:

```python
    from tradewind.strategy.actions import ActionError, parse_action
    from tradewind.strategy.conditions import ConditionError, parse_condition

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
```

(Move the imports to the top of the file in final form.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_actions.py tests/test_strategy_model.py -v` → all pass; full suite green.
- [ ] **Step 5: Commit** — `feat: action grammar, OrderIntent conversion, load-time rule validation`

---

### Task 5: Strategy store (files + version snapshots + rule states)

**Files:**
- Modify: `tradewind/store/db.py` (extend SCHEMA)
- Create: `tradewind/strategy/store.py`
- Test: `tests/test_strategy_store.py`

**Interfaces:**
- SCHEMA gains two idempotent tables:
  - `strategy_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL, version INTEGER NOT NULL, ts TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', content TEXT NOT NULL)`
  - `rule_states(strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL, state TEXT NOT NULL, updated_ts TEXT NOT NULL, PRIMARY KEY (strategy_id, rule_id))`
- `StrategyStore(dir: Path, conn: sqlite3.Connection)`:
  - `load_all(status: StrategyStatus | None = StrategyStatus.ACTIVE) -> list[StrategyDoc]` — loads every `*.yaml` in dir (sorted by name), filters by status (None = all), merges persisted rule states over the YAML defaults. A file failing validation raises `StrategyValidationError` (callers handle).
  - `load(strategy_id: str) -> StrategyDoc` — single file, with states merged.
  - `snapshot_version(doc: StrategyDoc, reason: str) -> None` — inserts full YAML text (re-serialized via `yaml.safe_dump` of `doc.model_dump(mode="json")`) into strategy_versions.
  - `versions(strategy_id: str) -> list[sqlite3.Row]` — newest first.
  - `set_rule_state(strategy_id: str, rule_id: str, state: RuleState) -> None` — upsert.
  - `rearm(strategy_id: str, rule_id: str) -> None` — convenience: set ARMED.

- [ ] **Step 1: Write the failing test**

`tests/test_strategy_store.py`:
```python
from pathlib import Path

import pytest

from tradewind.store.db import connect
from tradewind.strategy.loader import StrategyValidationError
from tradewind.strategy.model import RuleState, StrategyStatus
from tradewind.strategy.store import StrategyStore

ACTIVE = """
name: "A"
status: active
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""
DRAFT = """
name: "B"
status: draft
position: {ticker: MSFT, target_weight: 10%}
"""


@pytest.fixture()
def store(tmp_path: Path) -> StrategyStore:
    (tmp_path / "a.yaml").write_text(ACTIVE)
    (tmp_path / "b.yaml").write_text(DRAFT)
    return StrategyStore(tmp_path, connect(tmp_path / "t.db"))


def test_load_all_filters_active(store):
    docs = store.load_all()
    assert [d.id for d in docs] == ["a"]
    assert store.load_all(status=None).__len__() == 2


def test_rule_state_merge_and_rearm(store):
    store.set_rule_state("a", "r1", RuleState.TRIGGERED)
    [doc] = store.load_all()
    assert doc.rules[0].state == RuleState.TRIGGERED
    store.rearm("a", "r1")
    assert store.load("a").rules[0].state == RuleState.ARMED


def test_snapshot_and_versions(store):
    doc = store.load("a")
    store.snapshot_version(doc, reason="initial")
    doc2 = doc.model_copy(update={"version": 2})
    store.snapshot_version(doc2, reason="tighten stop")
    rows = store.versions("a")
    assert [r["version"] for r in rows] == [2, 1]
    assert "AAPL" in rows[0]["content"]


def test_invalid_file_raises(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: x\nstatus: active\n")
    s = StrategyStore(tmp_path, connect(tmp_path / "t.db"))
    with pytest.raises(StrategyValidationError):
        s.load_all()
```

- [ ] **Step 2: Run to verify it fails** — FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement**

Append to `SCHEMA` in `tradewind/store/db.py`:
```sql
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_states (
    strategy_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (strategy_id, rule_id)
);
```

`tradewind/strategy/store.py`:
```python
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tradewind.strategy.loader import load_strategy
from tradewind.strategy.model import RuleState, StrategyDoc, StrategyStatus


class StrategyStore:
    """Strategies live as YAML files (source of truth for definitions).
    Runtime rule state and version snapshots live in SQLite — the sentinel
    never rewrites the user's YAML."""

    def __init__(self, directory: Path, conn: sqlite3.Connection) -> None:
        self.directory = directory
        self._conn = conn

    def load_all(self, status: StrategyStatus | None = StrategyStatus.ACTIVE
                 ) -> list[StrategyDoc]:
        docs = []
        for path in sorted(self.directory.glob("*.yaml")):
            doc = self._merge_states(load_strategy(path))
            if status is None or doc.status == status:
                docs.append(doc)
        return docs

    def load(self, strategy_id: str) -> StrategyDoc:
        return self._merge_states(load_strategy(self.directory / f"{strategy_id}.yaml"))

    def _merge_states(self, doc: StrategyDoc) -> StrategyDoc:
        rows = self._conn.execute(
            "SELECT rule_id, state FROM rule_states WHERE strategy_id = ?",
            (doc.id,)).fetchall()
        states = {r["rule_id"]: RuleState(r["state"]) for r in rows}
        for rule in doc.rules:
            if rule.id in states:
                rule.state = states[rule.id]
        return doc

    def set_rule_state(self, strategy_id: str, rule_id: str, state: RuleState) -> None:
        self._conn.execute(
            "INSERT INTO rule_states (strategy_id, rule_id, state, updated_ts)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(strategy_id, rule_id) DO UPDATE SET state=excluded.state,"
            " updated_ts=excluded.updated_ts",
            (strategy_id, rule_id, state.value,
             datetime.now(timezone.utc).isoformat()))
        self._conn.commit()

    def rearm(self, strategy_id: str, rule_id: str) -> None:
        self.set_rule_state(strategy_id, rule_id, RuleState.ARMED)

    def snapshot_version(self, doc: StrategyDoc, reason: str) -> None:
        content = yaml.safe_dump(doc.model_dump(mode="json"), allow_unicode=True,
                                 sort_keys=False)
        self._conn.execute(
            "INSERT INTO strategy_versions (strategy_id, version, ts, reason, content)"
            " VALUES (?, ?, ?, ?, ?)",
            (doc.id, doc.version, datetime.now(timezone.utc).isoformat(),
             reason, content))
        self._conn.commit()

    def versions(self, strategy_id: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_id = ?"
            " ORDER BY version DESC", (strategy_id,)))
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_strategy_store.py -v` → 4 PASSED; full suite green (schema idempotency still covered by test_journal).
- [ ] **Step 5: Commit** — `feat: strategy store with version snapshots and persisted rule states`

---

### Task 6: Pending review queue (service API)

**Files:**
- Modify: `tradewind/store/db.py` (extend SCHEMA)
- Create: `tradewind/store/reviews.py`
- Test: `tests/test_reviews.py`

**Interfaces:**
- SCHEMA gains:
  - `pending_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL, ticker TEXT NOT NULL, rule_type TEXT NOT NULL, condition TEXT NOT NULL, action TEXT NOT NULL, snapshot TEXT NOT NULL, intent TEXT, status TEXT NOT NULL DEFAULT 'pending', resolved_ts TEXT, resolution_note TEXT, execution_result TEXT)`
- `ReviewError(Exception)`.
- `ReviewQueue(conn, executor: Executor)`:
  - `add(*, strategy_id, rule_id, ticker, rule_type: str, condition: str, action: str, snapshot: dict, intent: OrderIntent | None) -> int` — snapshot json-serialized (Decimals as str), intent via `intent.model_dump_json()` or NULL.
  - `list(status: str = "pending") -> list[sqlite3.Row]` — newest first; `status=None` → all.
  - `get(review_id: int) -> sqlite3.Row` — raises `ReviewError` if absent.
  - `approve(review_id: int) -> ExecutionResult` — only from `pending` and with a stored intent (else `ReviewError`); reconstructs `OrderIntent` (`model_validate_json`), calls `executor.execute`, stores outcome, sets status `approved`, records resolved_ts. If execution raises `ExecutionError`, status becomes `approved` with the error recorded in execution_result, and the exception propagates.
  - `reject(review_id: int, note: str = "") -> None` — only from `pending`.

- [ ] **Step 1: Write the failing test**

`tests/test_reviews.py`:
```python
import json
from decimal import Decimal

import pytest

from tradewind.broker.base import OrderIntent, OrderSide
from tradewind.store.db import connect
from tradewind.store.reviews import ReviewError, ReviewQueue


class StubExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent)
        from tradewind.risk.gate import RiskDecision
        from tradewind.execution import ExecutionResult
        return ExecutionResult(submitted=True, order=None,
                               decision=RiskDecision(approved=True))


INTENT = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("500"),
                     reason="dip", strategy_id="s1")


@pytest.fixture()
def queue(tmp_path):
    return ReviewQueue(connect(tmp_path / "t.db"), StubExecutor())


def add(queue, intent=INTENT):
    return queue.add(strategy_id="s1", rule_id="r1", ticker="AAPL",
                     rule_type="soft", condition="price < 205", action="buy $500",
                     snapshot={"price": Decimal("204.5")}, intent=intent)


def test_add_and_list(queue):
    rid = add(queue)
    [row] = queue.list()
    assert row["id"] == rid and row["status"] == "pending"
    assert json.loads(row["snapshot"])["price"] == "204.5"


def test_approve_executes_and_resolves(queue):
    rid = add(queue)
    result = queue.approve(rid)
    assert result.submitted
    assert queue._executor.calls[0].ticker == "AAPL"
    row = queue.get(rid)
    assert row["status"] == "approved" and row["resolved_ts"]
    assert queue.list() == []


def test_reject(queue):
    rid = add(queue)
    queue.reject(rid, note="not now")
    row = queue.get(rid)
    assert row["status"] == "rejected" and row["resolution_note"] == "not now"


def test_approve_twice_raises(queue):
    rid = add(queue)
    queue.approve(rid)
    with pytest.raises(ReviewError):
        queue.approve(rid)


def test_approve_without_intent_raises(queue):
    rid = add(queue, intent=None)
    with pytest.raises(ReviewError):
        queue.approve(rid)


def test_get_missing_raises(queue):
    with pytest.raises(ReviewError):
        queue.get(999)
```

- [ ] **Step 2: Run to verify it fails** — FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement**

Append to `SCHEMA` in `tradewind/store/db.py`:
```sql
CREATE TABLE IF NOT EXISTS pending_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    condition TEXT NOT NULL,
    action TEXT NOT NULL,
    snapshot TEXT NOT NULL,          -- JSON (Decimals as strings)
    intent TEXT,                     -- OrderIntent JSON, NULL if none
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|expired
    resolved_ts TEXT,
    resolution_note TEXT,
    execution_result TEXT            -- JSON summary after approve
);
```

`tradewind/store/reviews.py`:
```python
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from tradewind.broker.base import OrderIntent
from tradewind.execution import ExecutionError, ExecutionResult, Executor


class ReviewError(Exception):
    pass


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)}")


class ReviewQueue:
    """Service API for pending trigger reviews. The CLI today, the Web UI
    (Phase 5) and the agent (Phase 3) all operate this same interface."""

    def __init__(self, conn: sqlite3.Connection, executor: Executor) -> None:
        self._conn = conn
        self._executor = executor

    def add(self, *, strategy_id: str, rule_id: str, ticker: str, rule_type: str,
            condition: str, action: str, snapshot: dict,
            intent: OrderIntent | None) -> int:
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), strategy_id, rule_id, ticker,
             rule_type, condition, action,
             json.dumps(snapshot, default=_json_default),
             intent.model_dump_json() if intent else None))
        self._conn.commit()
        return cur.lastrowid

    def list(self, status: str | None = "pending") -> list[sqlite3.Row]:
        if status is None:
            return list(self._conn.execute(
                "SELECT * FROM pending_reviews ORDER BY id DESC"))
        return list(self._conn.execute(
            "SELECT * FROM pending_reviews WHERE status = ? ORDER BY id DESC",
            (status,)))

    def get(self, review_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM pending_reviews WHERE id = ?", (review_id,)).fetchone()
        if row is None:
            raise ReviewError(f"review {review_id} not found")
        return row

    def approve(self, review_id: int) -> ExecutionResult:
        row = self.get(review_id)
        if row["status"] != "pending":
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")
        if not row["intent"]:
            raise ReviewError(f"review {review_id} has no executable intent")
        intent = OrderIntent.model_validate_json(row["intent"])
        try:
            result = self._executor.execute(intent)
        except ExecutionError as exc:
            self._resolve(review_id, "approved",
                          execution_result=json.dumps({"error": str(exc)}))
            raise
        self._resolve(review_id, "approved",
                      execution_result=result.model_dump_json())
        return result

    def reject(self, review_id: int, note: str = "") -> None:
        row = self.get(review_id)
        if row["status"] != "pending":
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")
        self._resolve(review_id, "rejected", note=note)

    def _resolve(self, review_id: int, status: str, note: str = "",
                 execution_result: str | None = None) -> None:
        self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=?, resolution_note=?,"
            " execution_result=? WHERE id=?",
            (status, datetime.now(timezone.utc).isoformat(), note,
             execution_result, review_id))
        self._conn.commit()
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_reviews.py -v` → 6 PASSED; full suite green.
- [ ] **Step 5: Commit** — `feat: pending review queue service API`

---

### Task 7: Notification layer

**Files:**
- Create: `tradewind/notify/__init__.py`, `tradewind/notify/base.py`, `tradewind/notify/email.py`
- Modify: `tradewind/config.py` (SMTP + sentinel settings), `.env.example`
- Test: `tests/test_notify.py`

**Interfaces:**
- `Settings` gains: `smtp_host: str = ""`, `smtp_port: int = 587`, `smtp_user: str = ""`, `smtp_password: str = ""`, `smtp_from: str = ""`, `notify_to: str = ""`, `sentinel_interval_minutes: int = 60`, `strategies_dir: Path = Path("strategies")`.
- `Notifier(ABC)` with `send(subject: str, body: str) -> None`.
- `ConsoleNotifier(Notifier)` — prints `[notify] {subject}` + body to stdout.
- `EmailNotifier(Notifier)` — `__init__(host, port, user, password, sender, to, smtp_factory=smtplib.SMTP)`; `send` builds `email.message.EmailMessage`, STARTTLS, login, send. `smtp_factory` injectable for tests.
- `build_notifier(settings) -> Notifier` — EmailNotifier when `smtp_host` and `notify_to` are both set, else ConsoleNotifier. Notification failures must never crash the caller: `send` exceptions are caught inside `EmailNotifier.send` and printed to stderr (notification-only channel; a failed email must not break the sentinel).

- [ ] **Step 1: Write the failing test**

`tests/test_notify.py`:
```python
from tradewind.config import Settings
from tradewind.notify.base import ConsoleNotifier
from tradewind.notify.email import EmailNotifier, build_notifier


class StubSMTP:
    instances = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sent = []
        StubSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_console_notifier_prints(capsys):
    ConsoleNotifier().send("subj", "body")
    out = capsys.readouterr().out
    assert "subj" in out and "body" in out


def test_email_notifier_sends():
    n = EmailNotifier("smtp.x.com", 587, "u", "p", "from@x.com", "to@x.com",
                      smtp_factory=StubSMTP)
    n.send("Trigger: AAPL", "details")
    smtp = StubSMTP.instances[-1]
    assert smtp.tls and smtp.creds == ("u", "p")
    [msg] = smtp.sent
    assert msg["Subject"] == "Trigger: AAPL"
    assert msg["To"] == "to@x.com"


def test_email_failure_does_not_raise(capsys):
    def broken(host, port):
        raise OSError("connection refused")

    n = EmailNotifier("smtp.x.com", 587, "u", "p", "f@x.com", "t@x.com",
                      smtp_factory=broken)
    n.send("s", "b")  # must not raise


def test_build_notifier_selects(tmp_path):
    s = Settings(_env_file=tmp_path / "none.env")
    assert isinstance(build_notifier(s), ConsoleNotifier)
    s2 = s.model_copy(update={"smtp_host": "smtp.x.com", "notify_to": "t@x.com"})
    assert isinstance(build_notifier(s2), EmailNotifier)
```

- [ ] **Step 2: Run to verify it fails** — FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement**

`tradewind/config.py` — add fields to `Settings`:
```python
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_to: str = ""
    sentinel_interval_minutes: int = 60
    strategies_dir: Path = Path("strategies")
```

`tradewind/notify/__init__.py`:
```python
from tradewind.notify.base import ConsoleNotifier, Notifier
from tradewind.notify.email import EmailNotifier, build_notifier

__all__ = ["ConsoleNotifier", "EmailNotifier", "Notifier", "build_notifier"]
```

`tradewind/notify/base.py`:
```python
from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, body: str) -> None: ...


class ConsoleNotifier(Notifier):
    def send(self, subject: str, body: str) -> None:
        print(f"[notify] {subject}\n{body}")
```

`tradewind/notify/email.py`:
```python
from __future__ import annotations

import smtplib
import sys
from email.message import EmailMessage
from typing import Callable

from tradewind.config import Settings
from tradewind.notify.base import ConsoleNotifier, Notifier


class EmailNotifier(Notifier):
    """Email is a notification-only channel: bodies never contain action
    links, and a send failure must never break the caller."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 sender: str, to: str,
                 smtp_factory: Callable = smtplib.SMTP) -> None:
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.sender, self.to = sender, to
        self._smtp = smtp_factory

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender or self.user
        msg["To"] = self.to
        msg.set_content(body)
        try:
            with self._smtp(self.host, self.port) as smtp:
                smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 — notification must not crash callers
            print(f"[notify] email send failed: {exc}", file=sys.stderr)


def build_notifier(settings: Settings) -> Notifier:
    if settings.smtp_host and settings.notify_to:
        return EmailNotifier(settings.smtp_host, settings.smtp_port,
                             settings.smtp_user, settings.smtp_password,
                             settings.smtp_from, settings.notify_to)
    return ConsoleNotifier()
```

`.env.example` — append:
```
# Email notifications (optional; console fallback when unset)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
NOTIFY_TO=

# Sentinel
SENTINEL_INTERVAL_MINUTES=60
STRATEGIES_DIR=strategies
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_notify.py tests/test_config.py -v` → all pass; full suite green.
- [ ] **Step 5: Commit** — `feat: notification layer (console + SMTP email) and sentinel settings`

---

### Task 8: Sentinel engine

**Files:**
- Create: `tradewind/sentinel.py`
- Test: `tests/test_sentinel.py`

**Interfaces:**
- `TriggerOutcome(BaseModel)`: `strategy_id: str`, `rule_id: str`, `disposition: str` (one of `executed | queued | notified | skipped | error`), `detail: str = ""`.
- `SentinelReport(BaseModel)`: `strategies_checked: int`, `outcomes: list[TriggerOutcome]`, `errors: list[str]`.
- `Sentinel(strategies: StrategyStore, data: DataSource, broker: Broker, executor: Executor, queue: ReviewQueue, notifier: Notifier)`:
  - `run_once() -> SentinelReport`. Flow per active strategy: quote → build ctx (`price`, `position_qty` = held qty, `position_weight` = qty*price/equity fresh, `avg_entry_price`, `pnl_pct` = (price-avg_entry)/avg_entry*100, all `Decimal("0")` when no position; `target_weight` from plan, derived `target_value/equity` in value mode) → for each rule with `state == ARMED`: `evaluate_condition`; on True:
    1. `strategies.set_rule_state(..., TRIGGERED)` FIRST (crash-safe: never double-execute),
    2. build intent via `to_order_intent` (reason = `"strategy {id} rule {rule_id}: {condition} -> {action}"`),
    3. dispatch per matrix: `notify` auth → disposition `notified`; `confirm` auth → `queue.add` + `queued`; `auto` auth: hard → `executor.execute` (`executed`; `ExecutionError` → disposition `error` with detail, sentinel continues), soft → `queue.add` + `queued`. Intent None → disposition `skipped` (still notified, e.g. "sell all triggered but no position").
    4. `notifier.send` for every trigger (subject `[tradewind] {strategy_id}/{rule_id} triggered`, body includes condition, action, price, disposition).
  - Account/positions fetched once per run. Per-strategy exceptions (quote failure, bad strategy file) append to `errors` and continue. No triggers → no notifications.

- [ ] **Step 1: Write the failing test**

`tests/test_sentinel.py`:
```python
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradewind.broker.base import (
    Account, Broker, Order, OrderStatus, Position,
)
from tradewind.data.base import Bar, DataSource, Quote
from tradewind.execution import ExecutionError
from tradewind.risk.gate import RiskDecision
from tradewind.sentinel import Sentinel
from tradewind.store.db import connect
from tradewind.store.reviews import ReviewQueue
from tradewind.strategy.model import RuleState
from tradewind.strategy.store import StrategyStore


def strategy_yaml(auth="auto", rule_type="hard", condition="price < 250",
                  action="sell all", status="active"):
    return f"""
name: "T"
status: {status}
authorization: {auth}
position: {{ticker: AAPL, target_weight: 15%}}
rules:
  - {{id: r1, type: {rule_type}, condition: "{condition}", action: "{action}"}}
"""


class FakeData(DataSource):
    def __init__(self, price="200"):
        self.price = Decimal(price)

    def get_quote(self, ticker):
        return Quote(ticker=ticker, price=self.price,
                     as_of=datetime.now(timezone.utc))

    def get_bars(self, ticker, days=365):
        return []


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def __init__(self, qty="10"):
        self.qty = Decimal(qty)

    def get_account(self):
        return Account(equity=Decimal("10000"), cash=Decimal("5000"),
                       buying_power=Decimal("10000"))

    def get_positions(self):
        if self.qty <= 0:
            return []
        return [Position(ticker="AAPL", qty=self.qty,
                         avg_entry_price=Decimal("180"),
                         market_value=self.qty * Decimal("200"),
                         unrealized_pl=Decimal("0"))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


class SpyExecutor:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def execute(self, intent):
        if self.fail:
            raise ExecutionError("boom")
        self.calls.append(intent)
        from tradewind.execution import ExecutionResult
        return ExecutionResult(submitted=True, order=None,
                               decision=RiskDecision(approved=True))


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


def make(tmp_path: Path, yaml_text: str, *, price="200", qty="10", fail=False):
    (tmp_path / "t.yaml").write_text(yaml_text)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    executor = SpyExecutor(fail=fail)
    queue = ReviewQueue(conn, executor)
    notifier = SpyNotifier()
    s = Sentinel(store, FakeData(price), FakeBroker(qty), executor, queue, notifier)
    return s, store, executor, queue, notifier


def test_no_trigger_no_noise(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(condition="price < 100"))
    report = s.run_once()
    assert report.strategies_checked == 1
    assert report.outcomes == [] and n.sent == [] and ex.calls == []


def test_hard_auto_executes_and_marks_triggered(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml())
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert len(ex.calls) == 1 and ex.calls[0].qty == Decimal("10")
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert len(n.sent) == 1
    # second run: rule stays triggered, nothing happens
    assert s.run_once().outcomes == []


def test_soft_auto_enqueues(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(rule_type="soft"))
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    assert ex.calls == [] and len(q.list()) == 1


def test_confirm_auth_enqueues_hard_rule(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(auth="confirm"))
    assert s.run_once().outcomes[0].disposition == "queued"
    assert ex.calls == []


def test_notify_auth_only_notifies(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(auth="notify"))
    assert s.run_once().outcomes[0].disposition == "notified"
    assert ex.calls == [] and q.list() == [] and len(n.sent) == 1


def test_no_position_sell_is_skipped(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(), qty="0")
    report = s.run_once()
    assert report.outcomes[0].disposition == "skipped"
    assert store.load("t").rules[0].state == RuleState.TRIGGERED


def test_execution_error_reported_not_raised(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(), fail=True)
    report = s.run_once()
    assert report.outcomes[0].disposition == "error"
    assert "boom" in report.outcomes[0].detail


def test_bad_quote_collects_error_and_continues(tmp_path):
    class BadData(FakeData):
        def get_quote(self, ticker):
            raise ValueError("no price")

    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor()
    s = Sentinel(store, BadData(), FakeBroker(), ex, ReviewQueue(conn, ex), SpyNotifier())
    report = s.run_once()
    assert report.errors and "no price" in report.errors[0]


def test_draft_strategy_ignored(tmp_path):
    s, *_ = make(tmp_path, strategy_yaml(status="draft"))
    assert s.run_once().strategies_checked == 0
```

- [ ] **Step 2: Run to verify it fails** — FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement**

`tradewind/sentinel.py`:
```python
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from tradewind.broker.base import Broker, OrderIntent, Position
from tradewind.data.base import DataSource
from tradewind.execution import ExecutionError, Executor
from tradewind.notify.base import Notifier
from tradewind.store.reviews import ReviewQueue
from tradewind.strategy.actions import parse_action, to_order_intent
from tradewind.strategy.conditions import evaluate_condition
from tradewind.strategy.model import (
    Authorization, RuleState, RuleType, StrategyDoc,
)
from tradewind.strategy.store import StrategyStore


class TriggerOutcome(BaseModel):
    strategy_id: str
    rule_id: str
    disposition: str  # executed | queued | notified | skipped | error
    detail: str = ""


class SentinelReport(BaseModel):
    strategies_checked: int = 0
    outcomes: list[TriggerOutcome] = []
    errors: list[str] = []


class Sentinel:
    """One monitoring pass: evaluate every armed rule of every active
    strategy and dispatch triggers per (rule type x authorization)."""

    def __init__(self, strategies: StrategyStore, data: DataSource,
                 broker: Broker, executor: Executor, queue: ReviewQueue,
                 notifier: Notifier) -> None:
        self.strategies = strategies
        self.data = data
        self.broker = broker
        self.executor = executor
        self.queue = queue
        self.notifier = notifier

    def run_once(self) -> SentinelReport:
        report = SentinelReport()
        try:
            docs = self.strategies.load_all()
            account = self.broker.get_account()
            positions = {p.ticker: p for p in self.broker.get_positions()}
        except Exception as exc:  # noqa: BLE001 — a broken env must surface, not crash
            report.errors.append(f"setup failed: {exc}")
            return report

        for doc in docs:
            try:
                self._check_strategy(doc, account.equity, positions, report)
                report.strategies_checked += 1
            except Exception as exc:  # noqa: BLE001 — isolate per-strategy failures
                report.errors.append(f"{doc.id}: {exc}")
        return report

    def _check_strategy(self, doc: StrategyDoc, equity: Decimal,
                        positions: dict[str, Position],
                        report: SentinelReport) -> None:
        ticker = doc.position.ticker
        quote = self.data.get_quote(ticker)
        position = positions.get(ticker)
        ctx = self._build_ctx(doc, quote.price, position, equity)

        for rule in doc.rules:
            if rule.state != RuleState.ARMED:
                continue
            if not evaluate_condition(rule.condition, ctx):
                continue
            # One-shot: persist TRIGGERED before any execution attempt.
            self.strategies.set_rule_state(doc.id, rule.id, RuleState.TRIGGERED)
            outcome = self._dispatch(doc, rule.id, rule.type, rule.condition,
                                     rule.action, quote.price, position, equity,
                                     ctx)
            report.outcomes.append(outcome)
            self.notifier.send(
                f"[tradewind] {doc.id}/{rule.id} triggered",
                f"strategy: {doc.name}\nrule: {rule.id} ({rule.type.value})\n"
                f"condition: {rule.condition}\naction: {rule.action}\n"
                f"price: {quote.price}\ndisposition: {outcome.disposition}"
                + (f"\ndetail: {outcome.detail}" if outcome.detail else ""))

    @staticmethod
    def _build_ctx(doc: StrategyDoc, price: Decimal, position: Position | None,
                   equity: Decimal) -> dict[str, Decimal]:
        qty = position.qty if position else Decimal("0")
        avg = position.avg_entry_price if position else Decimal("0")
        weight = (qty * price / equity) if equity > 0 else Decimal("0")
        pnl_pct = ((price - avg) / avg * 100) if avg > 0 else Decimal("0")
        plan = doc.position
        target_weight = (plan.target_weight if plan.target_weight is not None
                         else (plan.target_value / equity if equity > 0
                               else Decimal("0")))
        return {"price": price, "position_qty": qty, "position_weight": weight,
                "avg_entry_price": avg, "pnl_pct": pnl_pct,
                "target_weight": target_weight}

    def _dispatch(self, doc: StrategyDoc, rule_id: str, rule_type: RuleType,
                  condition: str, action: str, price: Decimal,
                  position: Position | None, equity: Decimal,
                  ctx: dict[str, Decimal]) -> TriggerOutcome:
        reason = f"strategy {doc.id} rule {rule_id}: {condition} -> {action}"
        intent = to_order_intent(parse_action(action), strategy=doc,
                                 rule_id=rule_id, price=price,
                                 position=position, equity=equity, reason=reason)
        snapshot = {k: str(v) for k, v in ctx.items()}

        if intent is None:
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="no actionable order (e.g. no position)")
        if doc.authorization == Authorization.NOTIFY:
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="notified")
        if (doc.authorization == Authorization.AUTO
                and rule_type == RuleType.HARD):
            try:
                result = self.executor.execute(intent)
            except ExecutionError as exc:
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="error", detail=str(exc))
            detail = ("submitted" if result.submitted
                      else "; ".join(result.decision.reasons))
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="executed", detail=detail)
        # confirm auth (both types), or auto+soft
        self.queue.add(strategy_id=doc.id, rule_id=rule_id, ticker=doc.position.ticker,
                       rule_type=rule_type.value, condition=condition, action=action,
                       snapshot=snapshot, intent=intent)
        return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                              disposition="queued")
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_sentinel.py -v` → 10 PASSED; full suite green.
- [ ] **Step 5: Commit** — `feat: sentinel engine (one-shot triggers, authorization matrix dispatch)`

---

### Task 9: Scheduler, app wiring, CLI commands, example strategy

**Files:**
- Create: `tradewind/scheduler.py`, `tradewind/app.py`, `strategies/example.yaml`
- Modify: `tradewind/cli.py`, `pyproject.toml` (add `apscheduler>=3.10`)
- Test: `tests/test_scheduler.py`, `tests/test_cli_phase2.py`

**Interfaces:**
- `scheduler.is_market_hours(now: datetime | None = None) -> bool` — `America/New_York`, Mon–Fri, 09:30 ≤ t < 16:00. `now` naive → assumed UTC.
- `scheduler.run_daemon(sentinel_factory: Callable[[], Sentinel], interval_minutes: int, scheduler_cls=BlockingScheduler) -> None` — schedules a job every `interval_minutes` that calls `sentinel_factory().run_once()` only when `is_market_hours()`; prints report summary. `scheduler_cls` injectable for tests.
- `app.Components` (dataclass): `settings, broker, data, journal, gate, executor, queue, strategies, notifier, sentinel`.
- `app.build_components(settings: Settings, broker: Broker | None = None) -> Components` — wires everything (AlpacaBroker from settings unless injected; YFinanceSource; RiskLimits() defaults for now).
- CLI new subcommands (all through `main(argv, broker_factory=None)`):
  - `check` — one sentinel pass, print report (strategies checked, each outcome, errors). Exit 0; exit 1 if report.errors.
  - `run` — daemon via `run_daemon`.
  - `strategies` — list all strategies (any status): id, name, status, authorization, and each rule with state.
  - `rearm <strategy_id> <rule_id>` — re-arm; prints confirmation.
  - `reviews list` / `reviews approve <id>` / `reviews reject <id> [--note NOTE]`.
- `strategies/example.yaml` — a `status: draft` example (never runs) documenting the format.

- [ ] **Step 1: Write the failing tests**

`tests/test_scheduler.py`:
```python
from datetime import datetime, timezone

from tradewind.scheduler import is_market_hours

# 2026-07-29 is a Wednesday. 15:00 UTC = 11:00 ET (EDT, UTC-4).


def test_open_wednesday_11am_et():
    assert is_market_hours(datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc))


def test_closed_before_open():
    # 13:00 UTC = 09:00 ET < 09:30
    assert not is_market_hours(datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc))


def test_closed_after_close():
    # 20:30 UTC = 16:30 ET
    assert not is_market_hours(datetime(2026, 7, 29, 20, 30, tzinfo=timezone.utc))


def test_closed_weekend():
    # 2026-08-01 is a Saturday
    assert not is_market_hours(datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc))


def test_boundary_open_and_close():
    # 13:30 UTC = 09:30 ET exactly -> open; 20:00 UTC = 16:00 ET exactly -> closed
    assert is_market_hours(datetime(2026, 7, 29, 13, 30, tzinfo=timezone.utc))
    assert not is_market_hours(datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc))
```

`tests/test_cli_phase2.py`:
```python
from decimal import Decimal
from pathlib import Path

from tests.test_sentinel import FakeBroker  # reuse fixture broker
from tradewind.cli import main

STRAT = """
name: "T"
status: active
authorization: notify
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100000", action: "sell all"}
"""


def setup_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "t.yaml").write_text(STRAT)


def test_check_command_runs_and_reports(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    code = main(["check"], broker_factory=lambda s: FakeBroker())
    out = capsys.readouterr().out
    assert code == 0
    assert "t/r1" in out and "notified" in out


def test_strategies_command_lists_rules(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    code = main(["strategies"], broker_factory=lambda s: FakeBroker())
    out = capsys.readouterr().out
    assert code == 0
    assert "t" in out and "r1" in out and "armed" in out


def test_rearm_command(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    main(["check"], broker_factory=lambda s: FakeBroker())  # triggers r1
    code = main(["rearm", "t", "r1"], broker_factory=lambda s: FakeBroker())
    assert code == 0
    out = capsys.readouterr().out
    assert "armed" in out


def test_reviews_flow(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "strategies" / "t.yaml").write_text(
        STRAT.replace("authorization: notify", "authorization: confirm"))
    main(["check"], broker_factory=lambda s: FakeBroker())
    main(["reviews", "list"], broker_factory=lambda s: FakeBroker())
    out = capsys.readouterr().out
    assert "pending" in out
    code = main(["reviews", "reject", "1", "--note", "no"],
                broker_factory=lambda s: FakeBroker())
    assert code == 0
```

Note: `check`/`strategies`/`rearm`/`reviews` require broker credentials or an injected `broker_factory`, same pattern as `status` (exit 2 without either).

- [ ] **Step 2: Run to verify failures** — FAIL (ModuleNotFoundError / unknown command)

- [ ] **Step 3: Implement**

Add `"apscheduler>=3.10",` to `pyproject.toml` dependencies; run `uv sync`.

`tradewind/scheduler.py`:
```python
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from tradewind.sentinel import Sentinel

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def is_market_hours(now: datetime | None = None) -> bool:
    """US regular session, no holiday calendar yet (see docs/TODO.md)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(ET)
    return et.weekday() < 5 and OPEN <= et.time() < CLOSE


def run_daemon(sentinel_factory: Callable[[], Sentinel], interval_minutes: int,
               scheduler_cls: type = BlockingScheduler) -> None:
    def job() -> None:
        if not is_market_hours():
            print("[sentinel] market closed, skipping")
            return
        report = sentinel_factory().run_once()
        print(f"[sentinel] checked={report.strategies_checked} "
              f"triggers={len(report.outcomes)} errors={len(report.errors)}")
        for o in report.outcomes:
            print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition} {o.detail}")
        for e in report.errors:
            print(f"  error: {e}")

    scheduler = scheduler_cls()
    scheduler.add_job(job, "interval", minutes=interval_minutes,
                      next_run_time=datetime.now(timezone.utc))
    print(f"[tradewind] sentinel daemon: every {interval_minutes}min "
          "during US market hours (Ctrl-C to stop)")
    scheduler.start()
```

`tradewind/app.py`:
```python
from __future__ import annotations

from dataclasses import dataclass

from tradewind.broker.base import Broker
from tradewind.config import Settings
from tradewind.data.base import DataSource
from tradewind.data.yf import YFinanceSource
from tradewind.execution import Executor
from tradewind.notify.base import Notifier
from tradewind.notify.email import build_notifier
from tradewind.risk.gate import RiskGate, RiskLimits
from tradewind.sentinel import Sentinel
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal
from tradewind.store.reviews import ReviewQueue
from tradewind.strategy.store import StrategyStore


@dataclass
class Components:
    settings: Settings
    broker: Broker
    data: DataSource
    journal: TradeJournal
    gate: RiskGate
    executor: Executor
    queue: ReviewQueue
    strategies: StrategyStore
    notifier: Notifier
    sentinel: Sentinel


def build_components(settings: Settings, broker: Broker | None = None) -> Components:
    if broker is None:
        from tradewind.broker.alpaca import AlpacaBroker

        broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                              paper=settings.alpaca_paper)
    conn = connect(settings.db_path)
    data = YFinanceSource()
    journal = TradeJournal(conn)
    gate = RiskGate(RiskLimits())
    executor = Executor(broker, gate, journal, data)
    queue = ReviewQueue(conn, executor)
    settings.strategies_dir.mkdir(exist_ok=True)
    strategies = StrategyStore(settings.strategies_dir, conn)
    notifier = build_notifier(settings)
    sentinel = Sentinel(strategies, data, broker, executor, queue, notifier)
    return Components(settings=settings, broker=broker, data=data, journal=journal,
                      gate=gate, executor=executor, queue=queue,
                      strategies=strategies, notifier=notifier, sentinel=sentinel)
```

`tradewind/cli.py` — restructure `main` to build subcommands `status | check | run | strategies | rearm | reviews`. Keep `cmd_status` as-is. New handlers (complete `main` shown):

```python
from __future__ import annotations

import argparse
import sys
from typing import Callable

from tradewind.broker.base import Broker
from tradewind.config import Settings, SettingsStore
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal


def _default_broker(settings: Settings) -> Broker:
    from tradewind.broker.alpaca import AlpacaBroker

    return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                        paper=settings.alpaca_paper)


def cmd_status(settings: Settings, broker: Broker) -> int:
    # ... unchanged from Phase 1 ...


def cmd_check(components) -> int:
    report = components.sentinel.run_once()
    print(f"strategies checked: {report.strategies_checked}")
    for o in report.outcomes:
        print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition}"
              + (f" ({o.detail})" if o.detail else ""))
    for e in report.errors:
        print(f"  error: {e}", file=sys.stderr)
    return 1 if report.errors else 0


def cmd_strategies(components) -> int:
    docs = components.strategies.load_all(status=None)
    if not docs:
        print("no strategies found in", components.settings.strategies_dir)
        return 0
    for d in docs:
        print(f"{d.id}  [{d.status.value}/{d.authorization.value}]  {d.name}")
        for r in d.rules:
            print(f"    {r.id} ({r.type.value}, {r.state.value}): "
                  f"{r.condition} -> {r.action}")
    return 0


def cmd_rearm(components, strategy_id: str, rule_id: str) -> int:
    doc = components.strategies.load(strategy_id)
    if rule_id not in {r.id for r in doc.rules}:
        print(f"rule {rule_id} not found in {strategy_id}", file=sys.stderr)
        return 1
    components.strategies.rearm(strategy_id, rule_id)
    print(f"{strategy_id}/{rule_id} re-armed")
    return 0


def cmd_reviews(components, args) -> int:
    from tradewind.store.reviews import ReviewError

    q = components.queue
    try:
        if args.reviews_command == "list":
            rows = q.list()
            if not rows:
                print("no pending reviews")
            for r in rows:
                print(f"#{r['id']} {r['ts'][:19]} {r['strategy_id']}/{r['rule_id']} "
                      f"[{r['status']}] {r['condition']} -> {r['action']}")
        elif args.reviews_command == "approve":
            result = q.approve(args.review_id)
            print("executed" if result.submitted
                  else f"rejected by risk gate: {'; '.join(result.decision.reasons)}")
        else:
            q.reject(args.review_id, note=args.note or "")
            print(f"review {args.review_id} rejected")
        return 0
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None,
         broker_factory: Callable[[Settings], Broker] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradewind")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show account, positions, recent trades")
    sub.add_parser("check", help="run one sentinel pass now")
    sub.add_parser("run", help="run the sentinel daemon")
    sub.add_parser("strategies", help="list strategies and rule states")
    p_rearm = sub.add_parser("rearm", help="re-arm a triggered rule")
    p_rearm.add_argument("strategy_id")
    p_rearm.add_argument("rule_id")
    p_reviews = sub.add_parser("reviews", help="pending trigger reviews")
    rsub = p_reviews.add_subparsers(dest="reviews_command", required=True)
    rsub.add_parser("list")
    p_app = rsub.add_parser("approve")
    p_app.add_argument("review_id", type=int)
    p_rej = rsub.add_parser("reject")
    p_rej.add_argument("review_id", type=int)
    p_rej.add_argument("--note", default="")
    args = parser.parse_args(argv)

    settings = SettingsStore().load()
    if broker_factory is None and not (
            settings.alpaca_api_key and settings.alpaca_secret_key):
        print("Missing credentials: set ALPACA_API_KEY / ALPACA_SECRET_KEY "
              "in .env (see .env.example)", file=sys.stderr)
        return 2
    broker = (broker_factory or _default_broker)(settings)

    if args.command == "status":
        return cmd_status(settings, broker)

    from tradewind.app import build_components

    components = build_components(settings, broker=broker)
    if args.command == "check":
        return cmd_check(components)
    if args.command == "run":
        from tradewind.scheduler import run_daemon

        run_daemon(lambda: components.sentinel,
                   settings.sentinel_interval_minutes)
        return 0
    if args.command == "strategies":
        return cmd_strategies(components)
    if args.command == "rearm":
        return cmd_rearm(components, args.strategy_id, args.rule_id)
    if args.command == "reviews":
        return cmd_reviews(components, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

`strategies/example.yaml`:
```yaml
# Example strategy (status: draft => never evaluated by the sentinel).
# Copy to a new file, adjust, and set `status: active` to enable.
name: "AAPL long-term hold (example)"
status: draft
version: 1
authorization: confirm   # notify | confirm | auto

thesis: |
  Example: bullish on services growth, 12-18 month horizon.
  Invalidation: services revenue growth stalls two quarters running.

position:
  ticker: AAPL
  target_weight: 15%     # or target_value: $15000
  max_weight: 20%

rules:
  - id: stop-loss
    type: hard           # hard = executes without LLM (when authorization: auto)
    condition: "price < 185"
    action: "sell all"
  - id: take-profit-1
    type: soft
    condition: "pnl_pct > 30"
    action: "sell 50%"
  - id: add-on-dip
    type: soft
    condition: "price < 205 and position_weight < target_weight"
    action: "buy $3000"

review:
  cadence: daily
  invalidation: "services revenue growth < 10% two quarters in a row"
```

- [ ] **Step 4: Run** — `uv run pytest -v` → all pass (existing `tests/test_cli.py` must still pass — the credentials-gate behavior is unchanged); `uv run ruff check .` clean.
- [ ] **Step 5: Commit** — `feat: scheduler, app wiring, sentinel/strategies/reviews CLI, example strategy`

---

## Phase 2 Definition of Done

- `uv run pytest` green; `uv run ruff check .` clean.
- With a strategies dir and paper keys: `uv run tradewind check` evaluates rules and reports; `uv run tradewind strategies` lists states; triggered confirm-auth rules appear in `tradewind reviews list` and can be approved (executes via risk gate) or rejected.
- `uv run tradewind run` starts the hourly (configurable) daemon and skips outside US market hours.
- A triggered rule never fires twice without explicit `rearm`.
- README roadmap Phase 2 checked off (do in final review fix wave if reviewer agrees).

## Later phases

Phase 3 (agent core) consumes: `ReviewQueue` (agent reviews queued soft triggers), `StrategyStore.snapshot_version` (strategy revisions), `evaluate_condition` ctx builder (agent explains triggers). Phase 5 Web UI operates `ReviewQueue`/`StrategyStore` through the same APIs. Phase 6 fills the daily-review scheduler slot.
