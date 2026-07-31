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
