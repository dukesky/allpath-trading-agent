# Regime Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command (`allpath-trade regime set`) owns the paper account's regime — revision auto-apply AND order authorization together — stored in the database, logged, visible in the UI, and followed by new strategies.

**Architecture:** New module `allpath_trade/regime.py` (app_state key + `regime_events` table + effective-flag resolution + set/status). Reflection reads the effective flag at run time. CLI `regime show|set`. The strategy applier corrects a paper strategy's authorization to the regime after a write. The nav shows a regime chip; Pending/Reports show a mismatch banner.

**Tech Stack:** Python 3.11+, pydantic v2, sqlite, FastAPI + Jinja2, pytest (`uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-10-25-regime-switch-design.md` — read it first.

## Global Constraints

- Regime values are exactly `human` and `auto`. App-state key: `regime:{account}`.
- Effective revision auto-apply for an account = regime if set (`auto`→True, `human`→False); else for `shadow` the new `shadow_experiment_auto_apply_revisions` setting if not None; else `experiment_auto_apply_revisions`.
- `regime set` is refused for `shadow` with a clear error; `--by` is required.
- Only ACTIVE paper strategies whose authorization is `auto` or `confirm` are flipped; `notify` and non-active strategies are never touched.
- Every switch appends exactly one `regime_events` row. The table is created with `CREATE TABLE IF NOT EXISTS` in `store/db.py` SCHEMA (no migration).
- No restart is required for a switch to take effect.
- Behavior with no regime set must be exactly today's (existing tests unchanged).
- UI copy in English. Before every commit: `uv run pytest -q` green and `uv run ruff check <touched files>` adds no new findings. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

### Task 1: Regime module, events table, shadow setting, reflection uses the effective flag

**Files:**
- Create: `allpath_trade/regime.py`
- Modify: `allpath_trade/store/db.py` (SCHEMA), `allpath_trade/config.py` (new setting next to `experiment_auto_apply_revisions`), `allpath_trade/reflect.py` (~line 481)
- Test: `tests/test_regime.py` (new), `tests/test_reflect.py`

**Interfaces (produces):**

```python
# allpath_trade/regime.py
REGIMES = ("human", "auto")

def regime_key(account: str) -> str: ...            # f"regime:{account}"
def get_regime(app_state, account: str) -> str | None: ...
def effective_auto_apply(app_state, settings, account: str) -> bool: ...
def regime_authorization(regime: str) -> Authorization: ...  # human->CONFIRM, auto->AUTO

class RegimeError(Exception): ...

@dataclass
class RegimeStatus:
    account: str
    regime: str | None
    auto_apply: bool
    auto_apply_source: str           # "regime" | "shadow setting" | "global flag"
    authorization_counts: dict[str, int]   # active strategies only, by authorization value
    mismatches: list[str]            # strategy ids (active, auto/confirm, != regime)
    events: list[dict]               # last 10 regime_events rows, newest first

def set_regime(*, app_state, conn, strategies, account: str, regime: str,
               by: str, note: str = "") -> RegimeStatus: ...
def regime_status(*, app_state, conn, strategies, settings, account: str) -> RegimeStatus: ...
```

`set_regime`: validate `regime in REGIMES`, `account == "paper"` (else `RegimeError("regime set is paper-only: shadow mirrors a real account …")`), `by.strip()` non-empty; write app_state; for each `strategies.load_all(status=None)` doc with `status == active` and authorization in {AUTO, CONFIRM} and != target → `strategies.set_authorization(doc.id, target, reason=f"regime {regime} (set by {by})")`; insert one `regime_events` row (ts UTC ISO, strategies_flipped count); return `regime_status(...)`. Note: `regime_status` needs `settings`; `set_regime` may take `settings` too — add it as a keyword if needed and keep the call sites consistent.

SCHEMA addition (after `rule_fires`):

```sql
-- Paper-study regime switches (spec 2026-10-25-regime-switch-design.md).
CREATE TABLE IF NOT EXISTS regime_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    account TEXT NOT NULL,
    regime TEXT NOT NULL,
    by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    strategies_flipped INTEGER NOT NULL DEFAULT 0
);
```

Config: `shadow_experiment_auto_apply_revisions: bool | None = None` with a comment in the style of the `shadow_max_*` fields.

Reflection: replace `auto_apply = self.settings.experiment_auto_apply_revisions` with `effective_auto_apply(c.app_state, self.settings, c.account)` (check the bundle exposes `app_state`; `app.py` Components has it). Keep the surrounding comment accurate.

- [ ] **Step 1: Failing tests** in `tests/test_regime.py` (use a tmp sqlite via `store.db.connect`, a real `AppState`, a real `StrategyStore` on a tmp dir with a few YAML files — copy the YAML-writing pattern from `tests/test_strategy_store.py`):
  - precedence: no regime → global flag; shadow with shadow setting → that; regime set → regime wins over both;
  - `set_regime(..., regime="human")` flips active auto strategies to confirm, leaves notify and paused/draft/archived untouched, writes one event with the right flipped count; a second identical call flips 0 and still logs an event;
  - `set_regime` for shadow raises `RegimeError`; empty `by` raises; bad regime raises;
  - `regime_status` reports a mismatch when an active auto strategy exists under `human`;
  - events are newest-first and capped at 10.
  In `tests/test_reflect.py`, add one test that the reflector's auto-apply follows `regime:paper=auto` even when the global setting is False (find how existing tests exercise the auto-apply path — grep `experiment_auto_apply_revisions` there — and mirror it).
- [ ] **Step 2: Run — expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Focused tests, full suite, ruff.**
- [ ] **Step 5: Commit** — `feat: regime module — paper study regime in the database, effective revision auto-apply`

---

### Task 2: CLI `regime show|set`

**Files:** Modify `allpath_trade/cli.py` (new `cmd_regime`, parser, dispatch next to `breaker`); Test: `tests/test_cli_phase2.py` (or the file where `breaker` CLI is tested — grep `"breaker"` in tests).

**Behavior:**
- `allpath-trade regime show [--account paper|shadow]` → prints status (regime or "not set (falling back to …)", effective auto-apply + source, authorization counts, mismatches as `MISMATCH: <id> is <auth>, regime expects <auth>`, last events). Exit 0.
- `allpath-trade regime set human|auto --by NAME [--note TEXT] [--account paper]` → calls `set_regime`, prints `Regime set to <regime> by <by>; flipped N strategies.` plus the status. `RegimeError` → stderr `error: …`, exit 1.
- Like `breaker`, it needs no broker: build from `AppState(conn)` and the strategy store the same way the `breaker` branch does.
- Add both lines to the CLI help/epilog list next to the breaker lines.

- [ ] Steps: failing tests (show with no regime; set human flips and prints count; set for shadow exits 1; set without --by is an argparse error) → implement → tests/suite/ruff → commit `feat: allpath-trade regime show|set`.

---

### Task 3: Applied strategies follow the regime

**Files:** Modify `allpath_trade/strategy/apply.py` (after the `atomic_write_text` / version snapshot at the end of `apply`); Test: `tests/test_strategy_apply.py`.

**Behavior:** after a successful write, if the store's account is `paper`, a regime is set (`get_regime(AppState(store._conn), "paper")` — or pass an app_state into the factory if cleaner; keep call sites in `app.py` consistent), and the written doc's authorization is AUTO or CONFIRM and differs from `regime_authorization(regime)`, call `store.set_authorization(strategy_id, target, reason=f"follows regime {regime}")`. NOTIFY untouched. Shadow never affected. No regime → unchanged behavior.

- [ ] Steps: failing tests (chat draft new strategy with `authorization: auto` under `regime:paper=human` ends up `confirm` on disk with a version row whose reason mentions the regime; same draft with no regime stays `auto`; notify stays notify; shadow store unaffected) → implement → tests/suite/ruff → commit `feat: applied paper strategies follow the regime's authorization`.

---

### Task 4: Regime visible in the UI + docs

**Files:** Modify `allpath_trade/web/routes/dashboard.py` (`nav_context`), `allpath_trade/web/templates/base.html` (chip next to the account switcher), `reviews.html` and `reports.html` (mismatch banner); `CHANGELOG.md`; `docs/experiment-autonomous-run.md`. Test: `tests/test_web_reviews.py`, `tests/test_web_reports.py` (or wherever reports page tests live).

**Behavior:**
- `nav_context` adds `regime_chip` for the current account: paper → "Human-gated" / "Autonomous" / "Regime not set"; shadow → "Revisions: auto" / "Revisions: human" from the effective flag. Add `regime_mismatches` (list of ids, paper only). Compute via `regime_status`; must never raise (wrap; on error omit the chip).
- `base.html`: a small muted chip near the switcher, e.g. `<span class="regime-chip" title="...">Human-gated</span>`, with a tooltip explaining the definition (revisions + orders need approval / both automatic).
- `reviews.html` and `reports.html`: when `regime_mismatches` is non-empty, a warning banner: "Regime mismatch: <ids> do not match the <regime> regime. Run `allpath-trade regime set` to re-apply, or check the drawdown breaker."
- Docs: CHANGELOG entry; runbook section "Paper-study regimes (from 2026-10-25)" explaining the definition, the command, the event table, the rollout step, and that `.env`'s flag is only a fallback now.

- [ ] Steps: failing tests (chip text for human/auto/unset; banner appears with a mismatched active strategy and not otherwise) → implement → tests/suite/ruff → commit `feat: regime chip and mismatch banner; docs`.
