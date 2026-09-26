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


class StrategyHorizon(str, Enum):
    LONG = "long"
    MEDIUM = "medium"
    SWING = "swing"


class StrategyBias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def _to_decimal(raw: object, *, percent: bool) -> Decimal:
    """Accept 0.15 / '0.15' / '15%' (percent fields) / '$9,000' (value fields)."""
    if isinstance(raw, Decimal):
        return raw
    text = str(raw).strip().replace(",", "").replace("$", "")
    try:
        if text.endswith("%"):
            if not percent:
                raise ValueError(f"percent not allowed here: {raw!r}")
            return Decimal(text[:-1]) / Decimal(100)
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {raw!r}") from exc


REARM_MIN_MINUTES = 15
DEFAULT_MAX_FIRES_PER_DAY = 3
MAX_FIRES_PER_DAY_LIMIT = 20


class Rule(BaseModel):
    id: str
    type: RuleType
    condition: str
    action: str
    state: RuleState = RuleState.ARMED
    # Opt-in re-arming (spec 2026-09-26-rule-rearm-design.md). None = one-shot,
    # today's behavior. Minutes after a fire before the sentinel may re-arm it.
    rearm: int | None = None
    max_fires_per_day: int | None = None

    @field_validator("rearm", mode="before")
    @classmethod
    def _parse_rearm(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, bool):
            # ValueError, not TypeError: pydantic validators only convert
            # ValueError/AssertionError into a ValidationError.
            raise ValueError("rearm must be a duration like 60m or 2h")  # noqa: TRY004
        if isinstance(v, int):
            return v
        text = str(v).strip().lower()
        try:
            if text.endswith("m"):
                return int(text[:-1])
            if text.endswith("h"):
                return int(text[:-1]) * 60
            return int(text)
        except ValueError as exc:
            raise ValueError(f"rearm must be a duration like 60m or 2h, got {v!r}") from exc

    @model_validator(mode="after")
    def _rearm_consistent(self) -> Rule:
        if self.rearm is None:
            if self.max_fires_per_day is not None:
                raise ValueError("max_fires_per_day requires rearm")
            return self
        if self.rearm < REARM_MIN_MINUTES:
            raise ValueError(f"rearm must be at least {REARM_MIN_MINUTES}m")
        if self.max_fires_per_day is None:
            self.max_fires_per_day = DEFAULT_MAX_FIRES_PER_DAY
        if not 1 <= self.max_fires_per_day <= MAX_FIRES_PER_DAY_LIMIT:
            raise ValueError(f"max_fires_per_day must be 1-{MAX_FIRES_PER_DAY_LIMIT}")
        return self


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
    def _has_target(self) -> PositionPlan:
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
    # Optional, presentation-only metadata for the strategies page (glance-
    # ability chips) -- never guessed. Absent in YAML means None, which the
    # UI renders as "no chip" rather than inventing a value; only present
    # when a human or agent actually wrote one in.
    horizon: StrategyHorizon | None = None
    bias: StrategyBias | None = None
    position: PositionPlan
    rules: list[Rule] = []
    review: ReviewPolicy = ReviewPolicy()
    # A notification preference, not a trading parameter -- one of two
    # fields the web UI is allowed to write directly (see
    # web/routes/strategies.py's notify-email toggle; `status` is the
    # other, via that same module's lifecycle-transition route). Defaults
    # True so a YAML written before this field existed keeps notifying
    # exactly as it always did.
    notify_email: bool = True

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> StrategyDoc:
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self
