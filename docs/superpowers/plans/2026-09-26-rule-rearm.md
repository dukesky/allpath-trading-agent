# Rule Re-arm (Cooldown) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rules can opt into re-arming after a cooldown, up to a daily cap, so paper strategies trade repeatedly instead of firing once.

**Architecture:** Two optional `Rule` fields (`rearm` minutes, `max_fires_per_day`); authoring-time validation in the loader; a `rule_fires` log table; one re-arm check at the top of the sentinel's per-rule loop; agent guidance text.

**Tech Stack:** Python 3.11+, pydantic v2, sqlite, pytest (`uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-09-26-rule-rearm-design.md` — read it first.

## Global Constraints

- A rule WITHOUT `rearm` must behave exactly as today (one-shot). Every existing test stays green unchanged.
- `rearm` minimum 15 minutes; `max_fires_per_day` default 3 when `rearm` is set, allowed 1–20; `max_fires_per_day` without `rearm` is a validation error.
- Re-arming happens only while the US market is open, only from state `triggered` (never `disabled`), never for `buy_call`/`buy_put`.
- "Today" for the daily cap is the US/Eastern calendar day (`allpath_trade.market_hours.ET`).
- Authoring checks live behind `authoring=True` only; plain loading never enforces them.
- No change to the drawdown breaker, the reflection re-arm path, or option pending-queue behavior.
- Before every commit: `uv run pytest -q` fully green and `uv run ruff check <touched files>` adds no new findings. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Agent-facing and doc text in English.

---

### Task 1: Rule fields, position-weight cap helper, authoring validation

**Files:**
- Modify: `allpath_trade/strategy/model.py` (`Rule`, ~line 60)
- Modify: `allpath_trade/strategy/conditions.py` (new public helper)
- Modify: `allpath_trade/strategy/loader.py` (rule loop in `parse_strategy_text`, ~line 124)
- Test: `tests/test_strategy_model.py`, `tests/test_conditions.py`

**Interfaces:**
- Produces (`model.py`): `REARM_MIN_MINUTES = 15`, `DEFAULT_MAX_FIRES_PER_DAY = 3`, `MAX_FIRES_PER_DAY_LIMIT = 20`; `Rule.rearm: int | None` (minutes), `Rule.max_fires_per_day: int | None` (always set when `rearm` is set).
- Produces (`conditions.py`): `caps_position_weight(text: str) -> bool`.

- [ ] **Step 1: Failing tests.**

`tests/test_conditions.py`:

```python
from allpath_trade.strategy.conditions import caps_position_weight


def test_caps_position_weight_and_chain():
    assert caps_position_weight("price < 480 and position_weight < 0.3")
    assert caps_position_weight("position_weight <= 0.25")
    assert caps_position_weight("0.3 > position_weight and price < 480")
    assert caps_position_weight("0 < position_weight < 0.4")


def test_caps_position_weight_rejects_uncapped():
    assert not caps_position_weight("price < 480")
    assert not caps_position_weight("position_weight > 0.1")
    assert not caps_position_weight("price < 400 or position_weight < 0.3")
    assert not caps_position_weight("not position_weight > 0.3")


def test_caps_position_weight_or_needs_every_branch():
    assert caps_position_weight(
        "(price < 400 and position_weight < 0.3) or position_weight < 0.1")
```

`tests/test_strategy_model.py` (use the file's existing imports/helpers for building YAML; `parse_strategy_text` and `StrategyValidationError` are already used there):

```python
REARM_BASE = """
name: "T"
status: active
authorization: auto
position: {{ticker: AAPL, target_weight: 15%}}
rules:
  - {{id: r1, type: hard, condition: "{condition}", action: "{action}"{extra}}}
"""


def _rearm_doc(condition="price < 480 and position_weight < 0.3", action="buy $10000",
               extra=", rearm: 60m", authoring=False):
    return parse_strategy_text("t", REARM_BASE.format(condition=condition, action=action,
                                                      extra=extra), authoring=authoring)


def test_rearm_parses_minutes_hours_and_bare_int():
    assert _rearm_doc(extra=", rearm: 60m").rules[0].rearm == 60
    assert _rearm_doc(extra=", rearm: 2h").rules[0].rearm == 120
    assert _rearm_doc(extra=", rearm: 90").rules[0].rearm == 90


def test_rearm_defaults_daily_cap_to_three():
    assert _rearm_doc().rules[0].max_fires_per_day == 3


def test_rule_without_rearm_has_no_cap():
    r = _rearm_doc(extra="").rules[0]
    assert r.rearm is None and r.max_fires_per_day is None


@pytest.mark.parametrize("extra", [
    ", rearm: 5m",                              # below 15-minute minimum
    ", rearm: soon",                            # unparseable
    ", max_fires_per_day: 3",                   # cap without rearm
    ", rearm: 60m, max_fires_per_day: 0",
    ", rearm: 60m, max_fires_per_day: 21",
])
def test_rearm_invalid_values_rejected(extra):
    with pytest.raises(StrategyValidationError):
        _rearm_doc(extra=extra)


def test_authoring_rejects_rearm_on_option_buy():
    with pytest.raises(StrategyValidationError, match="cannot re-arm"):
        parse_strategy_text("t", REARM_BASE.format(
            condition="price < 480", action="buy_call $1000",
            extra=", rearm: 60m") + "  - {id: x, type: hard, condition: \"price > 600\","
            " action: \"close_options\"}\n", authoring=True)


def test_authoring_rejects_uncapped_rearming_buy():
    with pytest.raises(StrategyValidationError, match="position_weight"):
        _rearm_doc(condition="price < 480", authoring=True)


def test_authoring_accepts_capped_rearming_buy_and_uncapped_rearming_sell():
    _rearm_doc(authoring=True)
    _rearm_doc(condition="price > 520", action="sell 25%", authoring=True)


def test_plain_load_tolerates_uncapped_rearming_buy():
    assert _rearm_doc(condition="price < 480").rules[0].rearm == 60
```

(Check the exact sell grammar in `strategy/actions.py` — `sell 25%` is expected to parse as `SELL_PCT`; if the grammar differs, use the documented form.)

- [ ] **Step 2: Run — expect failures.** `uv run pytest tests/test_conditions.py tests/test_strategy_model.py -q`

- [ ] **Step 3: Implement.**

`model.py` — constants above `class Rule`, then:

```python
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
            raise ValueError("rearm must be a duration like 60m or 2h")
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
```

(`field_validator`/`model_validator` are already imported in model.py.)

`conditions.py` — after `evaluate_condition`:

```python
def caps_position_weight(text: str) -> bool:
    """True when the condition can only be true while position_weight is below
    some bound. Used to stop a re-arming buy rule from re-buying without limit:
    an `and` chain needs one capping term, an `or` needs every branch capped,
    and `not` is never treated as a cap."""
    return _caps(parse_condition(text).body)


def _is_position_weight(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "position_weight"


def _caps(node: ast.AST) -> bool:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return any(_caps(v) for v in node.values)
        return all(_caps(v) for v in node.values)
    if isinstance(node, ast.Compare):
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            if (_is_position_weight(left) and isinstance(op, (ast.Lt, ast.LtE))
                    and not _is_position_weight(right)):
                return True
            if (_is_position_weight(right) and isinstance(op, (ast.Gt, ast.GtE))
                    and not _is_position_weight(left)):
                return True
            left = right
    return False
```

`loader.py` — inside the `else:` branch of the per-rule `parse_action` try (where `action_spec` is bound), after the existing option block, add:

```python
            if authoring and rule.rearm is not None:
                if action_spec.kind in (ActionKind.BUY_CALL, ActionKind.BUY_PUT):
                    errors.append(f"rule {rule.id}: option buys cannot re-arm "
                                  "(remove rearm, or use a stock buy)")
                elif (action_spec.kind in (ActionKind.BUY_VALUE, ActionKind.BUY_TO_TARGET)
                      and not caps_position_weight(rule.condition)):
                    errors.append(
                        f"rule {rule.id}: a re-arming buy rule must cap position_weight "
                        "in its condition, e.g. `... and position_weight < 0.3`")
```

Import `caps_position_weight` next to `parse_condition`. If `parse_condition` raised for this rule, `caps_position_weight` would raise too — guard by only calling it when the condition parsed (track a local `condition_ok` flag set in the existing `parse_condition` try).

- [ ] **Step 4: Run focused tests, then the full suite; ruff on touched files.**
- [ ] **Step 5: Commit** — `feat: opt-in rule re-arm fields with authoring-time safety checks`

---

### Task 2: Fire log and sentinel re-arm

**Files:**
- Modify: `allpath_trade/store/db.py` (`SCHEMA`, after `rule_states`)
- Modify: `allpath_trade/strategy/store.py` (`StrategyStore`)
- Modify: `allpath_trade/sentinel.py` (`_check_strategy` loop ~line 319; new `_now`, `_should_rearm`)
- Test: `tests/test_strategy_store.py`, `tests/test_sentinel.py`

**Interfaces:**
- Consumes: `Rule.rearm`, `Rule.max_fires_per_day` (Task 1); `market_hours.ET`; sentinel's existing `_us_market_open_now()` (an autouse fixture in test_sentinel patches it to True).
- Produces (`StrategyStore`): `record_fire(strategy_id: str, rule_id: str, ts: datetime) -> None`; `last_fire(strategy_id: str, rule_id: str) -> datetime | None`; `fire_count_since(strategy_id: str, rule_id: str, since: datetime) -> int`. All account-scoped like `set_rule_state`.
- Produces (`sentinel.py`): module function `_now() -> datetime` (UTC, monkeypatchable).

- [ ] **Step 1: Failing tests.**

`tests/test_strategy_store.py` (reuse the file's store fixture/helper): record three fires at t0, t0+1h, t0+2h for (s1, r1) and one for (s1, r2); assert `last_fire(s1, r1) == t0+2h`, `fire_count_since(s1, r1, t0+30m) == 2`, `last_fire(s1, missing) is None`, and that a store for a different account sees none of them.

`tests/test_sentinel.py` — add a helper and tests. The file's `make(tmp_path, yaml_text)` returns `(s, store, executor, queue, notifier)`; `SpyExecutor.calls` records stock orders. FakeData price is 200, FakeBroker holds 10 AAPL.

```python
from datetime import timedelta

REARM_YAML = """
name: "T"
status: active
authorization: auto
position: {{ticker: AAPL, target_weight: 15%}}
rules:
  - {{id: r1, type: hard, condition: "price < 250 and position_weight < 0.9",
      action: "buy $500"{extra}}}
"""


class _Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock(datetime(2026, 9, 28, 14, 0, tzinfo=UTC))  # Mon 10:00 ET
    monkeypatch.setattr("allpath_trade.sentinel._now", c)
    return c


def test_rule_without_rearm_stays_one_shot(tmp_path, clock):
    s, _store, ex, _q, _n = make(tmp_path, REARM_YAML.format(extra=""))
    s.run_once()
    clock.now += timedelta(hours=3)
    s.run_once()
    assert len(ex.calls) == 1


def test_rearm_fires_again_after_cooldown_not_before(tmp_path, clock):
    s, store, ex, _q, _n = make(tmp_path, REARM_YAML.format(extra=", rearm: 60m"))
    s.run_once()
    clock.now += timedelta(minutes=30)
    s.run_once()
    assert len(ex.calls) == 1
    clock.now += timedelta(minutes=31)
    s.run_once()
    assert len(ex.calls) == 2
    assert store.fire_count_since("t", "r1", clock.now - timedelta(days=1)) == 2


def test_rearm_respects_daily_cap_and_resets_next_et_day(tmp_path, clock):
    s, _store, ex, _q, _n = make(
        tmp_path, REARM_YAML.format(extra=", rearm: 60m, max_fires_per_day: 2"))
    for _ in range(4):
        s.run_once()
        clock.now += timedelta(minutes=61)
    assert len(ex.calls) == 2
    clock.now = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)  # next ET day
    s.run_once()
    assert len(ex.calls) == 3


def test_no_rearm_while_market_closed(tmp_path, clock, monkeypatch):
    s, _store, ex, _q, _n = make(tmp_path, REARM_YAML.format(extra=", rearm: 60m"))
    s.run_once()
    monkeypatch.setattr("allpath_trade.sentinel._us_market_open_now", lambda: False)
    clock.now += timedelta(hours=2)
    s.run_once()
    assert len(ex.calls) == 1


def test_disabled_rule_never_rearms(tmp_path, clock):
    from allpath_trade.strategy.model import RuleState
    s, store, ex, _q, _n = make(tmp_path, REARM_YAML.format(extra=", rearm: 60m"))
    store.set_rule_state("t", "r1", RuleState.DISABLED)
    s.run_once()
    clock.now += timedelta(hours=2)
    s.run_once()
    assert ex.calls == []


def test_rearm_writes_an_observation(tmp_path, clock):
    # Use whatever observations fixture/helper test_sentinel already has for
    # the "sentinel" source; assert a "sentinel_rearm" row appears after the
    # second fire. If the file has no observations fake, build the Sentinel
    # with `observations=` the same way other tests in this file do.
    ...
```

Write the last test concretely using the file's existing observations pattern (grep `observations=` in test_sentinel.py). Also add an option-buy runtime test: a hand-written (non-authoring) YAML whose `buy_call $500` rule has `rearm: 60m` plus a `close_options` rule, run via `make_option(..., backend=FakeOptionsBackend(pick=_PICK))`; after the first fire and +2h, `executor.option_calls` still has exactly one buy.

- [ ] **Step 2: Run — expect failures.**

- [ ] **Step 3: Implement.**

`db.py` `SCHEMA`, right after the `rule_states` table:

```sql
-- Every rule fire (re-arming or not): drives rearm cooldown/daily caps
-- (spec 2026-09-26-rule-rearm-design.md) and records when each rule fired.
CREATE TABLE IF NOT EXISTS rule_fires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'paper',
    strategy_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_fires_rule
    ON rule_fires (account, strategy_id, rule_id, ts);
```

`store.py` (timestamps stored as UTC ISO strings, same as `rule_states.updated_ts`):

```python
    def record_fire(self, strategy_id: str, rule_id: str, ts: datetime) -> None:
        self._conn.execute(
            "INSERT INTO rule_fires (account, strategy_id, rule_id, ts) VALUES (?, ?, ?, ?)",
            (self._account, strategy_id, rule_id, ts.astimezone(UTC).isoformat()))
        self._conn.commit()

    def last_fire(self, strategy_id: str, rule_id: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT MAX(ts) AS ts FROM rule_fires"
            " WHERE account = ? AND strategy_id = ? AND rule_id = ?",
            (self._account, strategy_id, rule_id)).fetchone()
        return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None

    def fire_count_since(self, strategy_id: str, rule_id: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM rule_fires"
            " WHERE account = ? AND strategy_id = ? AND rule_id = ? AND ts >= ?",
            (self._account, strategy_id, rule_id, since.astimezone(UTC).isoformat())
        ).fetchone()
        return int(row["n"])
```

(ISO strings with the same `+00:00` offset compare correctly as text.)

`sentinel.py`:
- Add `from datetime import timedelta` (and `UTC` if not already imported) and `from allpath_trade.market_hours import ET`.
- Module level, next to `_us_market_open_now`:

```python
def _now() -> datetime:
    """Wall clock for re-arm cooldowns; a function so tests can move time."""
    return datetime.now(UTC)
```

- In `_check_strategy`, replace the first two lines of the rule loop body:

```python
        for rule in doc.rules:
            if (rule.state == RuleState.TRIGGERED and rule.rearm is not None
                    and self._should_rearm(doc.id, rule)):
                self.strategies.set_rule_state(doc.id, rule.id, RuleState.ARMED)
                rule.state = RuleState.ARMED
                if self.observations is not None:
                    self.observations.add(
                        "sentinel_rearm",
                        f"{doc.id}/{rule.id} re-armed after {rule.rearm}m cooldown",
                        subject=doc.position.ticker)
            if rule.state != RuleState.ARMED:
                continue
```

- Right after the existing one-shot line `self.strategies.set_rule_state(doc.id, rule.id, RuleState.TRIGGERED)`, add `self.strategies.record_fire(doc.id, rule.id, _now())`.
- New method:

```python
    def _should_rearm(self, strategy_id: str, rule: Rule) -> bool:
        """Spec 2026-09-26-rule-rearm-design.md, decision 2. Option buys never
        re-arm, even from a hand-edited file that authoring would have
        rejected: each re-fire would pick and stack another contract."""
        try:
            if parse_action(rule.action).kind in (ActionKind.BUY_CALL, ActionKind.BUY_PUT):
                return False
        except ActionError:
            return False
        if not _us_market_open_now():
            return False
        now = _now()
        last = self.strategies.last_fire(strategy_id, rule.id)
        if last is not None and now - last < timedelta(minutes=rule.rearm):
            return False
        day_start = now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return (self.strategies.fire_count_since(strategy_id, rule.id, day_start)
                < rule.max_fires_per_day)
```

Import `Rule` from `strategy.model` and `ActionError` from `strategy.actions` if not already imported.

- [ ] **Step 4: Run focused tests (`tests/test_sentinel.py tests/test_strategy_store.py tests/test_db*.py`), then the full suite; ruff (tests/test_sentinel.py has pre-existing findings — count before/after, add none).**
- [ ] **Step 5: Commit** — `feat: sentinel re-arms opted-in rules after their cooldown, with a fire log`

---

### Task 3: Agent guidance and docs

**Files:**
- Modify: `allpath_trade/agent/context.py` (new `REARM_NOTE`, add to `build_system_prompt` parts after `OPTIONS_ACTIONS_NOTE`)
- Modify: `CHANGELOG.md`, `docs/experiment-autonomous-run.md`
- Test: `tests/test_context.py`

- [ ] **Step 1: Failing test** in `tests/test_context.py`, mirroring `test_system_prompt_requires_english_output` (same prompt construction):

```python
    assert "## Re-arming rules" in prompt
    assert "rearm: 60m" in prompt
    assert "position_weight" in prompt and "max_fires_per_day" in prompt
```

- [ ] **Step 2: Implement** — `context.py`, with a short comment in the same style as the neighbouring notes (assembled-prompt content, not IDENTITY.md):

```python
REARM_NOTE = """\

## Re-arming rules
By default a rule fires once and then stays triggered until a revision re-arms it.
To let a rule fire repeatedly, add `rearm` (cooldown after each fire: `60m`, `2h`,
minimum 15m) and optionally `max_fires_per_day` (default 3, maximum 20), e.g.
  - {id: dip-buy, type: hard, condition: "price < 480 and position_weight < 0.30",
     action: "buy $10000", rearm: 60m, max_fires_per_day: 3}
Rules re-arm only during US market hours. A re-arming buy rule must cap
position_weight in its condition (as above) or it is rejected. Option buys
(buy_call/buy_put) cannot re-arm; close_options and stock sells can.
"""
```

- [ ] **Step 3: Docs.**
  - `CHANGELOG.md`, in the file's existing style: opt-in rule re-arm (`rearm`, `max_fires_per_day`), authoring checks, `rule_fires` log, `sentinel_rearm` observations; also the 2026-09-26 NaN-quote fix already on main (`07d53dd`) if not yet listed.
  - `docs/experiment-autonomous-run.md`: a section "Phase 3 — high-activity paper testing (from 2026-09-26)" stating the phase boundaries (baseline human-verify 2026-09-14 → 2026-09-25; phase 3 from 2026-09-26), the settings (paper back to `auto`; `MAX_ORDER_VALUE=25000`, `MAX_POSITION_WEIGHT=0.50`, `MAX_DAILY_TRADES=50`, shadow pinned; `SENTINEL_INTERVAL_MINUTES=15`; drawdown breaker unchanged at 15%), and what the re-arm feature changes. Note that the phase-2 baseline data must not be pooled with phase-3 data.
- [ ] **Step 4: Full suite, ruff.**
- [ ] **Step 5: Commit** — `docs: re-arm guidance for the agent; phase 3 runbook`
