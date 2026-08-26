# Two-Week Autonomous Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent run the paper account unattended for two weeks — reflection revisions auto-apply behind an experiment flag, a drawdown circuit breaker halts auto trading, and the Alpaca HTTP client gets a socket timeout.

**Architecture:** Three independent changes to the existing per-account component bundle. (A) an opt-in flag makes `Reflector` approve the revision rows its own session just queued, through the untouched applier/guard chain; (B) a new `DrawdownBreaker` in `risk/` runs at the top of every sentinel pass, tracking peak equity in `app_state` and demoting `auto`→`confirm` via a new narrow `StrategyStore.set_authorization` writer; (C) `AlpacaBroker` binds a default `timeout=` onto its `TradingClient`'s requests session.

**Tech Stack:** Python 3.12, pydantic v2, FastAPI/Jinja2, sqlite, pytest. Spec: `docs/superpowers/specs/2026-08-26-two-week-autonomous-run-design.md`.

## Global Constraints

- `EXPERIMENT_AUTO_APPLY_REVISIONS` defaults to **False** (`.env`-only, not on the settings page — same policy as `REFLECTION_MAX_ITERS`).
- Auto-apply must go through `ReviewQueue.approve` → the existing applier. Never bypass the freeze (revisions touch `thesis`+`rules` only; `authorization`/`status` changes are rejected), the byte-exact base staleness check, or version monotonicity. A failed check leaves the row `pending`.
- Auto-apply covers only rows queued **by the current reflection run** (`source='reflection'`), and only when the run stores an "ok" report. Chat-sourced proposals and order proposals still require human approval.
- The breaker is per-account, trips **once** (no re-demote/re-alert loops), is disabled at `drawdown_halt_pct=0`, and recovery is manual (`allpath-trade breaker reset`). Demotion goes auto→confirm only — the breaker never touches `notify` strategies, `status`, or anything else in the YAML.
- yfinance already carries its own 30s timeout (`YfData.get(timeout=30)`) — Change C touches **only** the Alpaca client. Do not add a session wrapper to `data/yf.py`.
- All user-visible copy in English. Run the full suite (`uv run pytest -q`) before every commit; ~2194 tests must stay green.

---

### Task 1: Settings fields

**Files:**
- Modify: `allpath_trade/config.py` (Settings class, near `llm_timeout_seconds` at ~line 165)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.experiment_auto_apply_revisions: bool` (default False), `Settings.drawdown_halt_pct: Decimal` (default `Decimal("0.15")`, `ge=0`, `lt=1`), `Settings.broker_http_timeout_seconds: int` (default 30, `ge=5`). Env names follow pydantic-settings convention: `EXPERIMENT_AUTO_APPLY_REVISIONS`, `DRAWDOWN_HALT_PCT`, `BROKER_HTTP_TIMEOUT_SECONDS`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`, following its existing Settings-construction pattern)

```python
def test_experiment_and_breaker_settings_defaults():
    s = Settings(_env_file=None)
    assert s.experiment_auto_apply_revisions is False
    assert s.drawdown_halt_pct == Decimal("0.15")
    assert s.broker_http_timeout_seconds == 30


def test_drawdown_halt_pct_range(monkeypatch):
    monkeypatch.setenv("DRAWDOWN_HALT_PCT", "1.5")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("DRAWDOWN_HALT_PCT", "0")
    assert Settings().drawdown_halt_pct == Decimal("0")
```

(Import `Decimal` / `ValidationError` at the top if not already there; match how the file already constructs `Settings` — if existing tests use `Settings(_env_file=None)` or a helper, mirror it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -q -k "experiment or drawdown"`
Expected: FAIL (unknown attribute / unexpected keyword)

- [ ] **Step 3: Add the three fields to `Settings`**

```python
    # Two-week-autonomous-run experiment (spec 2026-08-26): reflection's own
    # revision proposals auto-apply through the normal applier when this is
    # on. .env-only by design -- flipping it is an experiment decision, not
    # a settings-page toggle a user should reach for casually.
    experiment_auto_apply_revisions: bool = False
    # Drawdown circuit breaker: halt auto trading when equity falls this
    # fraction below its recorded peak. 0 disables the breaker entirely.
    drawdown_halt_pct: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1)
    # Alpaca's TradingClient issues requests with NO socket timeout (see
    # docs/TODO.md's _broker_pool note) -- one hung call stalls a sentinel
    # tick silently. yfinance already defaults to 30s internally.
    broker_http_timeout_seconds: int = Field(default=30, ge=5)
```

(`from decimal import Decimal` — check config.py's imports.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/config.py tests/test_config.py
git commit -m "feat: settings for auto-apply experiment, drawdown breaker, broker timeout"
```

---

### Task 2: Alpaca HTTP timeout (Change C)

**Files:**
- Modify: `allpath_trade/broker/alpaca.py:38-41` (`AlpacaBroker.__init__`)
- Modify: `allpath_trade/app.py:152` (pass the setting)
- Test: `tests/test_broker_alpaca.py`

**Interfaces:**
- Consumes: `Settings.broker_http_timeout_seconds` (Task 1).
- Produces: `AlpacaBroker(api_key, secret_key, paper=True, client=None, http_timeout_seconds: float = 30.0)`.

**Background for the implementer:** alpaca-py 0.43.5's `RESTClient.__init__` creates `self._session: Session = Session()` (a `requests.Session`) and `_one_request` calls `self._session.request(method, url, **opts)` with **no** timeout kwarg, ever. Binding a default via `functools.partial` is therefore safe (no duplicate-kwarg risk). `TradingClient.__init__` does no network I/O, so tests can construct one with fake keys.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_broker_alpaca.py`)

```python
import functools
from types import SimpleNamespace


def test_own_trading_client_session_gets_default_timeout():
    b = AlpacaBroker("key", "secret", paper=True, http_timeout_seconds=7)
    req = b._client._session.request
    assert isinstance(req, functools.partial)
    assert req.keywords == {"timeout": 7.0}


def test_injected_client_session_is_left_untouched():
    session = SimpleNamespace(request=lambda *a, **k: None)
    original = session.request
    fake = SimpleNamespace(_session=session)
    AlpacaBroker("k", "s", client=fake)
    assert fake._session.request is original
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_broker_alpaca.py -q -k timeout`
Expected: FAIL (unexpected keyword `http_timeout_seconds`)

- [ ] **Step 3: Implement in `AlpacaBroker.__init__`**

```python
    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 client: object | None = None,
                 http_timeout_seconds: float = 30.0) -> None:
        self.is_paper = paper
        self._client = client or TradingClient(api_key, secret_key, paper=paper)
        if client is None:
            # alpaca-py's RESTClient never passes timeout= to its requests
            # session, so a hung TCP connection blocks forever -- and the
            # sentinel/dashboard threads behind it (docs/TODO.md). Binding a
            # default here fixes every call site at once; an injected test
            # client keeps whatever behavior it came with. getattr-guarded
            # so a future alpaca-py that renames _session degrades to the
            # old (no-timeout) behavior instead of crashing at startup.
            session = getattr(self._client, "_session", None)
            if session is not None:
                session.request = functools.partial(
                    session.request, timeout=float(http_timeout_seconds))
```

Add `import functools` to the module imports. In `app.py:152`, change the construction to:

```python
        return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                            paper=True,
                            http_timeout_seconds=settings.broker_http_timeout_seconds)
```

(Keep whatever other args line 152 currently passes — read it first.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_broker_alpaca.py tests/test_app.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/broker/alpaca.py allpath_trade/app.py tests/test_broker_alpaca.py
git commit -m "fix: default socket timeout on the Alpaca trading client"
```

---

### Task 3: Revision-proposal collector hook (Change A, part 1)

**Files:**
- Modify: `allpath_trade/agent/reflection_tools.py` (`register_reflection_tools`, ~line 30; the `rid = queue.add_strategy_revision(...)` site at ~line 163)
- Test: `tests/test_reflection_tools.py`

**Interfaces:**
- Produces: `register_reflection_tools(registry, *, strategies, queue, on_proposed: Callable[[int], None] | None = None)` — `on_proposed` is called with the new review row's id (plain `int`; `ReviewHandle` is an `int` subclass) after each successful `propose_strategy_revision`. Existing callers (reflect.py, tests) pass nothing and see no behavior change.

- [ ] **Step 1: Write the failing test** (append to `tests/test_reflection_tools.py`, reusing that file's existing registry/queue/strategy-file setup — read its current tests first and copy the setup of whichever test exercises a *successful* proposal)

```python
def test_on_proposed_receives_the_new_review_id(...existing fixture args...):
    # same arrange as the existing successful-proposal test, plus:
    seen: list[int] = []
    registry = ToolRegistry()
    register_reflection_tools(registry, strategies=strategies, queue=queue,
                              on_proposed=seen.append)
    result = registry.call("propose_strategy_revision", {...same valid args...})
    assert not result.startswith("error:")
    assert len(seen) == 1
    row = queue.get(seen[0])
    assert row["kind"] == "strategy_revision"


def test_on_proposed_not_called_on_a_rejected_proposal(...):
    seen: list[int] = []
    registry = ToolRegistry()
    register_reflection_tools(registry, strategies=strategies, queue=queue,
                              on_proposed=seen.append)
    registry.call("propose_strategy_revision",
                  {"strategy_id": "missing", "new_yaml": "x", "rationale": "r"})
    assert seen == []
```

(The `...` markers mean: copy the concrete arrange code from the neighboring passing test in that file — do not invent a new harness. The registry call surface may be `registry.call(name, args)` or similar; match the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reflection_tools.py -q -k on_proposed`
Expected: FAIL (unexpected keyword `on_proposed`)

- [ ] **Step 3: Implement**

Add the parameter and, immediately after the existing `rid = queue.add_strategy_revision(...)` call inside `propose_strategy_revision`:

```python
        if on_proposed is not None:
            on_proposed(int(rid))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reflection_tools.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/agent/reflection_tools.py tests/test_reflection_tools.py
git commit -m "feat: on_proposed hook reports queued revision ids to the caller"
```

---

### Task 4: Reflector auto-apply (Change A, part 2)

**Files:**
- Modify: `allpath_trade/reflect.py` (`Reflector._run` ~line 443-467 for the collector wiring; new `_auto_apply_revisions` method; the success path right after `report_id = c.reports.add(...)` ~line 545)
- Test: `tests/test_reflect.py`

**Interfaces:**
- Consumes: `Settings.experiment_auto_apply_revisions` (Task 1), `register_reflection_tools(on_proposed=...)` (Task 3), `ReviewQueue.approve(rid)` / `RevisionValidationError` / `ReviewError` (existing, `store/reviews.py`), `StrategyStore.rearm_warning(strategy_id)` (existing), `ObservationLog.add(source, text, subject=None)` (existing).
- Produces: observations with `source="reflection_auto_apply"`; approved revision rows on the success path.

- [ ] **Step 1: Wire the collector in `_run`**

Replace the existing `register_reflection_tools(registry, strategies=c.strategies, queue=c.queue)` line with:

```python
        proposed_rids: list[int] = []
        auto_apply = self.settings.experiment_auto_apply_revisions
        register_reflection_tools(
            registry, strategies=c.strategies, queue=c.queue,
            on_proposed=proposed_rids.append if auto_apply else None)
```

- [ ] **Step 2: Add the apply step on the success path**

Immediately after `report_id = c.reports.add(..., status="ok")` (BEFORE the notifier block — the report row is durably stored, which is the gate the spec sets; a `_fail` run must never reach this line):

```python
        if auto_apply and proposed_rids:
            self._auto_apply_revisions(proposed_rids)
```

- [ ] **Step 3: Implement `_auto_apply_revisions`**

```python
    def _auto_apply_revisions(self, rids: list[int]) -> None:
        """Experiment mode (spec 2026-08-26): approve the revision rows THIS
        run just queued, through the exact applier chain a human approval
        uses -- every guard (freeze, byte-exact base, version monotonicity)
        still decides. A guard rejection leaves the row pending, same as
        today, and the paper trail lands in observations either way."""
        c = self.components
        for rid in rids:
            try:
                row = dict(c.queue.get(rid))
            except ReviewError:
                continue  # superseded/vanished between propose and now
            if row["status"] != "pending" or row["kind"] != "strategy_revision":
                continue
            strategy_id = row["strategy_id"]
            try:
                c.queue.approve(rid)
            except (RevisionValidationError, ReviewError) as exc:
                c.observations.add(
                    "reflection_auto_apply",
                    f"revision #{rid} for {strategy_id} NOT auto-applied "
                    f"({exc}); left pending for human review",
                    subject=row["ticker"])
                continue
            warning = c.strategies.rearm_warning(strategy_id)
            c.observations.add(
                "reflection_auto_apply",
                f"revision #{rid} for {strategy_id} auto-applied "
                f"(experiment mode).{warning}",
                subject=row["ticker"])
```

Imports to add at the top of reflect.py: `from allpath_trade.store.reviews import ReviewError, RevisionValidationError` (check what's already imported).

- [ ] **Step 4: Write the tests** (append to `tests/test_reflect.py`; reuse `make_components`, `make_settings`, `ScriptedLLM`, `tool_response`, `STRAT`, `NOW`, `REPORT_TEXT` already defined there)

Prepare a valid revision of `STRAT` (version bumped, a rule price changed):

```python
STRAT_V2 = STRAT.replace("version: 1", "version: 2").replace("price < 100",
                                                             "price < 95")


def _scripted_propose_then_report(strategy_id="t"):
    return ScriptedLLM([
        tool_response("propose_strategy_revision", {
            "strategy_id": strategy_id, "new_yaml": STRAT_V2,
            "rationale": "tighten the stop"}),
        REPORT_TEXT,
    ])


def test_auto_apply_applies_this_runs_revision(tmp_path):
    components = make_components(tmp_path)
    # write STRAT to components' strategies dir as t.yaml -- copy the exact
    # file-writing arrange from test_run_daily_happy_path...
    settings = make_settings(experiment_auto_apply_revisions=True)
    r = Reflector(llm=_scripted_propose_then_report(), components=components,
                  settings=settings, notifier=None)
    out = r.run_daily(NOW)
    assert out.startswith("ok:")
    path = components.strategies.directory / "t.yaml"
    assert "version: 2" in path.read_text()
    (row,) = [dict(x) for x in components.queue.list(status=None)
              if x["kind"] == "strategy_revision"]
    assert row["status"] == "approved"
    texts = [o["text"] for o in components.observations.list()]  # match the
    # ObservationLog read API used elsewhere in this test file
    assert any("auto-applied" in t for t in texts)


def test_auto_apply_off_leaves_row_pending(tmp_path):
    # same arrange, settings WITHOUT the flag
    ...
    assert row["status"] == "pending"
    assert "version: 1" in path.read_text()


def test_auto_apply_skipped_when_run_fails(tmp_path):
    # ScriptedLLM: propose, then garbage, then garbage corrective ->
    # run_daily returns the _fail string; row must stay pending.
    ...


def test_auto_apply_guard_rejection_leaves_pending_and_observes(tmp_path):
    # Arrange a stale row directly: queue.add_strategy_revision with
    # old_yaml=STRAT, then overwrite t.yaml with different text, then call
    # r._auto_apply_revisions([rid]) -- byte-exact base check fires.
    ...
    assert row["status"] == "pending"
    assert any("NOT auto-applied" in t for t in texts)
```

(The `...` bodies repeat the first test's arrange with the stated difference — write them out fully in the actual test file. Match `queue.list` / `observations` read signatures to what `tests/test_reflect.py` and `tests/test_sentinel.py` already use.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_reflect.py -q`
Expected: PASS (new tests + all pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add allpath_trade/reflect.py tests/test_reflect.py
git commit -m "feat: reflection revisions auto-apply behind the experiment flag"
```

---

### Task 5: StrategyStore.set_authorization (breaker's write path)

**Files:**
- Modify: `allpath_trade/strategy/store.py` (new method after `rearm`, ~line 95)
- Test: `tests/test_strategy_store.py`

**Interfaces:**
- Produces: `StrategyStore.set_authorization(strategy_id: str, authorization: Authorization, reason: str) -> None` — rewrites the YAML's `authorization` field only, snapshots a version. Raises `FileNotFoundError`/`StrategyValidationError` as `load` would; callers decide how to isolate.

- [ ] **Step 1: Write the failing test** (append to `tests/test_strategy_store.py`, reusing its store fixture/strategy-file helpers)

```python
def test_set_authorization_rewrites_only_that_field(store_fixture...):
    # arrange: write a strategy YAML with authorization: auto, version 3
    store.set_authorization("s1", Authorization.CONFIRM, "drawdown breaker")
    doc = store.load("s1")
    assert doc.authorization == Authorization.CONFIRM
    assert doc.version == 3            # untouched
    assert doc.status == StrategyStatus.ACTIVE  # untouched
    versions = store.versions("s1")
    assert versions[-1]["reason"] == "drawdown breaker"  # match the actual
    # column name used by snapshot_version/versions in this file's tests
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_strategy_store.py -q -k set_authorization`
Expected: FAIL (no attribute `set_authorization`)

- [ ] **Step 3: Implement** — mirror `web/routes/strategies.py:230-239`'s raw-file-re-parse pattern (writing the SQLite-merged doc back would bake triggered rule state into the YAML):

```python
    def set_authorization(self, strategy_id: str, authorization: Authorization,
                          reason: str) -> None:
        """Narrow system write path (drawdown breaker): flip `authorization`
        and nothing else. Same raw-file re-parse as the web status route --
        never serialize the DB-merged doc back into the YAML."""
        path = self.directory / f"{strategy_id}.yaml"
        current = parse_strategy_text(strategy_id, path.read_text())
        updated = current.model_copy(update={"authorization": authorization})
        new_text = yaml.safe_dump(updated.model_dump(mode="json"),
                                  sort_keys=False, allow_unicode=True)
        atomic_write_text(path, new_text)
        self.snapshot_version(updated, reason)
```

Imports: `yaml`, `parse_strategy_text`, `atomic_write_text`, `Authorization` — check what store.py already imports from `strategy.loader` / `strategy.model` and add the missing ones.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_strategy_store.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/strategy/store.py tests/test_strategy_store.py
git commit -m "feat: StrategyStore.set_authorization for system demotions"
```

---

### Task 6: DrawdownBreaker (Change B core)

**Files:**
- Create: `allpath_trade/risk/breaker.py`
- Test: `tests/test_breaker.py` (new file)

**Interfaces:**
- Consumes: `AppState.get/set/delete` (`store/app_state.py`), `StrategyStore.load_all(status=None, errors=[])` + `set_authorization` (Task 5), `Authorization` enum.
- Produces:

```python
class BreakerTrip(BaseModel):
    peak: Decimal
    equity: Decimal
    drawdown: Decimal          # fraction, e.g. Decimal("0.16")
    demoted: list[str]         # strategy ids flipped auto -> confirm

class DrawdownBreaker:
    def __init__(self, app_state: AppState, strategies: StrategyStore,
                 halt_pct: Decimal, account: str) -> None: ...
    def tripped_at(self) -> str | None: ...   # ISO ts when tripped, else None
    def check(self, equity: Decimal) -> BreakerTrip | None: ...
    def reset(self) -> None: ...
```

App-state keys: `drawdown_peak:{account}`, `drawdown_tripped:{account}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_breaker.py`; build a real sqlite `AppState` and `StrategyStore` the way `tests/test_app_state.py` / `tests/test_strategy_store.py` do — copy their minimal fixtures)

```python
def _breaker(tmp_path, halt="0.15", account="paper", strategies_yaml=None):
    # returns (breaker, app_state, store); strategies_yaml is a dict of
    # id -> yaml text written into the store directory first
    ...


def test_no_trip_below_threshold(tmp_path):
    b, state, _ = _breaker(tmp_path)
    assert b.check(Decimal("100000")) is None       # sets peak
    assert b.check(Decimal("90000")) is None        # -10% < 15%
    assert state.get("drawdown_peak:paper") == "100000"


def test_peak_ratchets_up(tmp_path):
    b, state, _ = _breaker(tmp_path)
    b.check(Decimal("100000"))
    b.check(Decimal("120000"))
    assert state.get("drawdown_peak:paper") == "120000"


def test_trip_demotes_auto_strategies_once(tmp_path):
    b, state, store = _breaker(tmp_path, strategies_yaml={
        "a": AUTO_STRAT, "n": NOTIFY_STRAT})
    b.check(Decimal("100000"))
    trip = b.check(Decimal("80000"))                # -20%
    assert trip is not None
    assert trip.demoted == ["a"]
    assert store.load("a").authorization == Authorization.CONFIRM
    assert store.load("n").authorization == Authorization.NOTIFY  # untouched
    assert b.tripped_at() is not None
    assert b.check(Decimal("70000")) is None        # already tripped: silent


def test_disabled_at_zero(tmp_path):
    b, state, _ = _breaker(tmp_path, halt="0")
    assert b.check(Decimal("100000")) is None
    assert state.get("drawdown_peak:paper") is None  # fully inert


def test_reset_clears_peak_and_tripped(tmp_path):
    b, state, _ = _breaker(tmp_path, strategies_yaml={"a": AUTO_STRAT})
    b.check(Decimal("100000")); b.check(Decimal("80000"))
    b.reset()
    assert state.get("drawdown_peak:paper") is None
    assert b.tripped_at() is None
    # after reset the next check starts a fresh peak
    assert b.check(Decimal("80000")) is None


def test_accounts_are_isolated(tmp_path):
    # paper trips; a shadow breaker sharing the same app_state does not
    ...
```

Define `AUTO_STRAT` / `NOTIFY_STRAT` as small YAML literals (copy the shape of `STRAT` in tests/test_reflect.py, with `authorization: auto` / `authorization: notify`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_breaker.py -q`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement `allpath_trade/risk/breaker.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from allpath_trade.store.app_state import AppState
from allpath_trade.strategy.model import Authorization
from allpath_trade.strategy.store import StrategyStore


class BreakerTrip(BaseModel):
    peak: Decimal
    equity: Decimal
    drawdown: Decimal
    demoted: list[str]


class DrawdownBreaker:
    """Account-level kill-switch: when equity falls more than `halt_pct`
    below its recorded peak, demote every `auto` strategy to `confirm` --
    once -- and report the trip so the sentinel can alert. Peak and tripped
    state live in app_state (`drawdown_peak:{account}`,
    `drawdown_tripped:{account}`); recovery is a manual
    `allpath-trade breaker reset` after the user reviews the account."""

    def __init__(self, app_state: AppState, strategies: StrategyStore,
                 halt_pct: Decimal, account: str) -> None:
        self.app_state = app_state
        self.strategies = strategies
        self.halt_pct = halt_pct
        self.account = account
        self._peak_key = f"drawdown_peak:{account}"
        self._tripped_key = f"drawdown_tripped:{account}"

    def tripped_at(self) -> str | None:
        return self.app_state.get(self._tripped_key)

    def check(self, equity: Decimal) -> BreakerTrip | None:
        if self.halt_pct <= 0 or equity <= 0:
            return None
        if self.tripped_at() is not None:
            return None
        raw = self.app_state.get(self._peak_key)
        peak = Decimal(raw) if raw else equity
        if equity > peak:
            peak = equity
        self.app_state.set(self._peak_key, str(peak))
        drawdown = (peak - equity) / peak
        if drawdown <= self.halt_pct:
            return None
        demoted: list[str] = []
        errors: list[str] = []
        for doc in self.strategies.load_all(status=None, errors=errors):
            if doc.authorization != Authorization.AUTO:
                continue
            self.strategies.set_authorization(
                doc.id, Authorization.CONFIRM,
                f"drawdown breaker: {drawdown:.1%} below peak {peak}")
            demoted.append(doc.id)
        self.app_state.set(self._tripped_key,
                           datetime.now(UTC).isoformat())
        return BreakerTrip(peak=peak, equity=equity, drawdown=drawdown,
                           demoted=demoted)

    def reset(self) -> None:
        self.app_state.delete(self._peak_key)
        self.app_state.delete(self._tripped_key)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_breaker.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/risk/breaker.py tests/test_breaker.py
git commit -m "feat: drawdown circuit breaker core"
```

---

### Task 7: Breaker wiring — sentinel, notification, app assembly

**Files:**
- Modify: `allpath_trade/notify/events.py` (new `drawdown_halt` builder, after `order_result`)
- Modify: `allpath_trade/sentinel.py` (`__init__` new param; `run_once` after the equity/positions fetch at ~line 96)
- Modify: `allpath_trade/app.py` (`_build_account_components`, before the `Sentinel(...)` construction at ~line 207)
- Test: `tests/test_sentinel.py`, `tests/test_events.py` (or wherever events builders are tested — find with `grep -l "rule_triggered" tests/`)

**Interfaces:**
- Consumes: `DrawdownBreaker`/`BreakerTrip` (Task 6), `Settings.drawdown_halt_pct` (Task 1), `push_telegram_receipt` (already imported in sentinel.py).
- Produces: `events.drawdown_halt(*, account, peak, equity, drawdown, demoted) -> tuple[str, str]`; `Sentinel(..., breaker: DrawdownBreaker | None = None)`.

- [ ] **Step 1: Write the failing event-builder test**

```python
def test_drawdown_halt_names_the_damage_and_the_recovery():
    subject, body = events.drawdown_halt(
        account="paper", peak=Decimal("100000"), equity=Decimal("82000"),
        drawdown=Decimal("0.18"), demoted=["tsla-dip", "nvda-core"])
    assert "TRADING HALTED" in subject
    assert "[Paper]" in subject
    assert "18.0%" in body
    assert "tsla-dip, nvda-core" in body
    assert "breaker reset" in body
```

- [ ] **Step 2: Implement `events.drawdown_halt`**

```python
def drawdown_halt(*, account: str, peak: Decimal, equity: Decimal,
                  drawdown: Decimal, demoted: list[str]) -> tuple[str, str]:
    subject = (f"{_prefix(account)}[AllPath] TRADING HALTED: "
               f"{drawdown:.1%} drawdown")
    names = ", ".join(demoted) if demoted else "none were set to auto"
    body = (f"Equity ${equity:,.2f} is {drawdown:.1%} below its peak "
            f"${peak:,.2f}.\n"
            f"The drawdown circuit breaker tripped. Auto strategies demoted "
            f"to confirm: {names}.\n"
            "No further orders will execute without your approval.\n"
            "To resume: review the account, restore strategies to auto "
            "deliberately, then run `allpath-trade breaker reset`." + FOOTER)
    return subject, body
```

(Match `Decimal` import and `FOOTER` usage to the file's existing builders.)

- [ ] **Step 3: Write the failing sentinel tests** (append to `tests/test_sentinel.py`, reusing its fake broker/notifier/store fixtures — read the file's existing `Sentinel(...)` constructions and mirror one)

```python
def test_breaker_trip_halts_notifies_and_records(...):
    # arrange: fake broker equity 80_000; breaker pre-seeded with peak
    # 100_000 (call breaker.check(Decimal("100000")) once first, or set the
    # app_state key directly); one auto strategy on file
    report = sentinel.run_once()
    assert any("drawdown breaker" in e for e in report.errors)
    assert notifier.sent  # subject contains "TRADING HALTED"
    # the demoted strategy is evaluated as confirm on this same pass:
    # a triggering rule must land in the queue, not execute
    ...


def test_no_breaker_means_no_behavior_change(...):
    # Sentinel constructed without breaker= runs exactly as before
    ...


def test_tripped_breaker_does_not_realert(...):
    # second run_once after a trip: no second "TRADING HALTED" send
    ...
```

- [ ] **Step 4: Implement the sentinel wiring**

`__init__`: add `breaker: DrawdownBreaker | None = None` (import under `TYPE_CHECKING` or directly; follow the module's import style) and `self.breaker = breaker`.

In `run_once`, right after the `positions = {...}` line and before `docs = self.strategies.load_all(...)`:

```python
        if self.breaker is not None:
            trip = self.breaker.check(account.equity)
            if trip is not None:
                subject, body = events.drawdown_halt(
                    account=self.account, peak=trip.peak, equity=trip.equity,
                    drawdown=trip.drawdown, demoted=trip.demoted)
                if self.notifier is not None:
                    self.notifier.send(subject, body)
                push_telegram_receipt(
                    app_state=self.app_state,
                    telegram_bot_token=self.telegram_bot_token, body=body,
                    account=self.account)
                if self.observations is not None:
                    self.observations.add(
                        "breaker",
                        f"drawdown breaker tripped: {trip.drawdown:.1%} below "
                        f"peak {trip.peak}; demoted: "
                        f"{', '.join(trip.demoted) or 'none'}")
                report.errors.append(
                    f"drawdown breaker tripped ({trip.drawdown:.1%}); "
                    "auto strategies demoted to confirm")
```

(The demotion already happened inside `check`, so `load_all` below this point sees `confirm` docs — the same pass stops auto-executing. A dedicated observation source `"breaker"`, not `"sentinel"`, for the same digest-count reason documented at sentinel.py:122-129.)

In `app.py`'s `_build_account_components`, before the `sentinel = Sentinel(...)` line:

```python
    breaker = (DrawdownBreaker(app_state, strategies,
                               settings.drawdown_halt_pct, account)
               if app_state is not None else None)
```

and pass `breaker=breaker` to `Sentinel(...)`. Import `DrawdownBreaker` alongside the existing `from allpath_trade.risk.gate import ...` import.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_sentinel.py tests/test_app.py -q` (plus the events test file)
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add allpath_trade/notify/events.py allpath_trade/sentinel.py allpath_trade/app.py tests/
git commit -m "feat: sentinel trips the drawdown breaker and alerts every channel"
```

---

### Task 8: Breaker surfaces — CLI reset + dashboard banner

**Files:**
- Modify: `allpath_trade/cli.py` (new `breaker` subcommand; follow the `p_rearm`/`cmd_rearm` pattern at ~lines 128/552)
- Modify: `allpath_trade/web/routes/dashboard.py` + its template (`allpath_trade/web/templates/dashboard.html` — confirm the template name from the route's `TemplateResponse` call)
- Test: `tests/test_cli.py` (or `tests/test_cli_phase2.py` — wherever `rearm` is tested), `tests/test_web_dashboard.py`

**Interfaces:**
- Consumes: `DrawdownBreaker` (Task 6).
- Produces: `allpath-trade breaker status [--account paper|shadow]` and `allpath-trade breaker reset [--account ...]`; a dashboard banner when `drawdown_tripped:{account}` is set.

- [ ] **Step 1: Write the failing CLI test** (mirror how the existing `rearm`/`reviews` CLI tests build stores and call the cmd function directly)

```python
def test_cmd_breaker_status_and_reset(...):
    # arrange a tripped breaker (set both app_state keys)
    assert cmd_breaker(breaker, "status") == 0     # prints tripped + peak
    assert cmd_breaker(breaker, "reset") == 0
    assert breaker.tripped_at() is None


def test_cmd_breaker_reset_when_not_tripped_is_a_noop(...):
    assert cmd_breaker(breaker, "reset") == 0
```

- [ ] **Step 2: Implement `cmd_breaker` + parser wiring**

```python
def cmd_breaker(breaker, action: str) -> int:
    tripped = breaker.tripped_at()
    peak = breaker.app_state.get(breaker._peak_key)
    if action == "status":
        if tripped:
            print(f"TRIPPED at {tripped} (peak {peak})")
        else:
            print(f"ok (peak {peak or 'not yet recorded'})")
        return 0
    breaker.reset()
    print("breaker reset: peak and tripped state cleared. Strategies stay "
          "at confirm -- restore auto deliberately via the agent.")
    return 0
```

Parser: `p_breaker = sub.add_parser("breaker", help="drawdown circuit breaker status/reset")`, a positional `action` with `choices=["status", "reset"]`, and `--account` defaulting to `"paper"` (match how other subcommands take an account, if any do — otherwise add the flag here). In `main()`'s dispatch, build the breaker from the same components the other commands use (`app_state` + that account's `StrategyStore` + `settings.drawdown_halt_pct`).

- [ ] **Step 3: Write the failing dashboard test**

```python
def test_dashboard_shows_halt_banner_when_tripped(client_fixture...):
    # set app_state "drawdown_tripped:paper" to an ISO timestamp
    page = client.get("/").text
    assert "trading halted" in page.lower()


def test_dashboard_no_banner_by_default(...):
    assert "trading halted" not in client.get("/").text.lower()
```

- [ ] **Step 4: Implement the banner**

In the dashboard route, read `tripped = app_state.get(f"drawdown_tripped:{account}")` (using however the route currently accesses `app_state` and the active account — mirror the sentinel-heartbeat read nearby) and pass `breaker_tripped=tripped` into the template context. In the template, next to the sentinel heartbeat line:

```html
{% if breaker_tripped %}
<div class="banner banner-error">
  🚨 Trading halted — drawdown breaker tripped at {{ breaker_tripped }}.
  Auto strategies were demoted to confirm; run <code>allpath-trade breaker
  reset</code> after reviewing.
</div>
{% endif %}
```

(Match the existing banner/alert CSS classes used by the setup-wizard banner rather than inventing new ones.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_cli.py tests/test_web_dashboard.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add allpath_trade/cli.py allpath_trade/web/ tests/
git commit -m "feat: breaker status/reset CLI and dashboard halt banner"
```

---

### Task 9: Docs — runbook, changelog, TODO update

**Files:**
- Create: `docs/experiment-autonomous-run.md`
- Modify: `CHANGELOG.md`, `docs/TODO.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Write `docs/experiment-autonomous-run.md`** — the operator's runbook, containing exactly:
  1. Pre-flight: `git tag experiment-start`, back up `allpath-trade.db*`, `memory/`, `strategies/`; reset the Alpaca paper account to $100k in their dashboard.
  2. `.env` for the run: `EXPERIMENT_AUTO_APPLY_REVISIONS=true`, `SENTINEL_INTERVAL_MINUTES=30`, `DRAWDOWN_HALT_PCT=0.15`.
  3. Keep-alive: run `caffeinate -s uv run allpath-trade serve` (or a launchd `KeepAlive` plist — include a minimal plist example).
  4. The paragraph to append to `IDENTITY.md` for the run (verbatim, ready to copy):
     > **Experiment mode (temporary):** you are running a two-week autonomous
     > validation on the paper account. Be more active than a typical
     > mid/long-term posture: every nightly reflection MUST review each rule
     > that triggered (burned) that day and either re-arm it at a price level
     > you re-justify, or rewrite it. Do not leave a strategy with no armed
     > rules overnight without stating why in the report.
  5. Kickoff-chat checklist: budget ($100k), risk appetite, 5–8 tickers, ask for `authorization: auto` on each strategy, batch-approve, verify on the Strategies page that all show ACTIVE + auto.
  6. During the run: notifications are receive-only; manual pause = end of the zero-intervention claim for that strategy (note it); `allpath-trade breaker status` any time.
  7. Wrap-up: `git tag experiment-end`, second DB backup, remove the IDENTITY.md paragraph, flip the `.env` flags back, then build the report (separate task).

- [ ] **Step 2: Update `CHANGELOG.md`** — one entry covering: experiment auto-apply flag (default off), drawdown circuit breaker + CLI + banner, Alpaca client socket timeout.

- [ ] **Step 3: Update `docs/TODO.md`** — mark the broker-HTTP-timeout item (the `_broker_pool` note at ~line 75) as addressed by `broker_http_timeout_seconds`, same done-annotation style the file used for the llm-timeout item at ~line 96.

- [ ] **Step 4: Full suite + commit**

Run: `uv run pytest -q`
Expected: all green

```bash
git add docs/ CHANGELOG.md
git commit -m "docs: autonomous-run runbook; changelog and TODO updates"
```
