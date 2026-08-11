# Phase 6: Daily Reflection Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each trading day's close, a full-capability agent session reviews the day (fills, triggers, price action vs strategy assumptions) and produces an archived report, memory lessons, and human-approved strategy revision proposals.

**Architecture:** Reuse the AgentSession machinery (Option 2 + reins: 12-iteration cap, fenced seed briefing, dedicated `kind="reflection"` conversation). Report persists in a new `reports` table surfaced on a new Reports page; strategy proposals ride the existing `pending_reviews` queue with a `kind` discriminator and a revision-applier injection mirroring the `_executor` pattern. Spec: `docs/superpowers/specs/2026-08-10-phase6-reflection-design.md`.

**Tech Stack:** existing only — FastAPI/Starlette, SQLite via LockedConnection, pydantic, htmx (vendored). No new dependencies.

## Global Constraints

- `uv run pytest -q` and `uv run ruff check .` (line length 100) clean before every commit; never pipe pytest's exit code away.
- All UI text English (`tests/helpers.py::assert_english_only` on every changed page).
- Web layer never calls `Executor.execute()` directly; the reflection session has NO order tools and NO confirm tools.
- Memory writes only via `register_memory_tools`' guarded `memory_update`; all external material fenced with `fence_external`.
- No trading parameter editable from any page; revisions apply only through user approval on Pending.
- Notification failures never break the caller; daily jobs are isolated per-task (one failing never blocks the others).
- Schema changes: new tables go in `SCHEMA` (`CREATE TABLE IF NOT EXISTS` runs on every connect); new columns on existing tables go in `_MIGRATIONS` (ALTER TABLE, guarded).

---

### Task 1: Fill details in the trade journal + reports store

**Files:**
- Modify: `allpath_trade/store/db.py` (SCHEMA: `reports` table; `_MIGRATIONS`: trades fill columns)
- Modify: `allpath_trade/store/journal.py` (record fills; new `refresh_fill`)
- Modify: `allpath_trade/execution.py` (post-submit fill refresh)
- Create: `allpath_trade/store/reports.py`
- Modify: `allpath_trade/app.py` (Components gains `reports: ReportStore`)
- Test: `tests/test_journal.py`, `tests/test_reports_store.py`, `tests/test_execution.py`

**Interfaces:**
- Consumes: `Order.filled_qty: Decimal`, `Order.filled_avg_price: Decimal | None` (broker/base.py:67-76); `Broker.get_order(order_id)`.
- Produces:
  - trades columns `filled_qty TEXT`, `filled_avg_price TEXT` (NULL until known).
  - `TradeJournal.record(...)` unchanged signature, now persists `order.filled_qty`/`order.filled_avg_price` when `order` given.
  - `TradeJournal.refresh_fill(trade_id: int, order: Order) -> None` — updates the two fill columns + status from a re-fetched Order.
  - `class ReportStore` (store/reports.py): `add(date: str, body: str, summary: str, conversation_id: int | None, model: str, tokens_used: int, status: str = "ok") -> int`; `get(date: str) -> sqlite3.Row | None`; `list(limit: int = 90) -> list[sqlite3.Row]`; `exists(date: str) -> bool`. `date` is ET `YYYY-MM-DD`, UNIQUE. `status` is `ok` | `failed` (failed rows carry the error line in `body`).
  - `Components.reports: ReportStore` wired in `build_components`.

- [ ] Step 1: failing tests — journal persists fills from a FILLED Order; NULL for order=None; `refresh_fill` updates row; ReportStore add/get/exists/list ordering + UNIQUE date raises on duplicate add.
- [ ] Step 2: run, verify fail.
- [ ] Step 3: implement — `_MIGRATIONS` entries `("trades", "filled_qty", "TEXT")`, `("trades", "filled_avg_price", "TEXT")` following the existing tuple shape in db.py; `reports` table in SCHEMA:

```sql
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    summary TEXT NOT NULL,
    conversation_id INTEGER,
    model TEXT NOT NULL DEFAULT '',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL
);
```

- [ ] Step 4: Executor.execute success path (`execution.py:70` area): after `journal.record(intent, decision, order)`, if `order.status` is not FILLED, one `self.broker.get_order(order.id)` re-fetch in try/except (a failed refresh keeps the submitted row untouched — comment why: market orders often fill within the round-trip; one poll catches the common case, the reflection briefing tells the truth either way via NULL) then `journal.refresh_fill(trade_id, refreshed)`.
- [ ] Step 5: suite green; ruff; commit `feat: fill details in trade journal + reports store`.

### Task 2: Conversation kinds + review-queue kinds

**Files:**
- Modify: `allpath_trade/store/db.py` (`_MIGRATIONS`: `conversations.kind`, `pending_reviews.kind`)
- Modify: `allpath_trade/store/conversations.py`, `allpath_trade/store/reviews.py`
- Test: `tests/test_conversations.py`, `tests/test_reviews.py`

**Interfaces:**
- Produces:
  - `conversations.kind TEXT NOT NULL DEFAULT 'chat'`; `ConversationStore.start(kind: str = "chat") -> int`; `ConversationStore.latest(kind: str = "chat") -> int | None` (filters by kind so the web chat never resumes a reflection transcript).
  - `pending_reviews.kind TEXT NOT NULL DEFAULT 'order'`.
  - `ReviewQueue.add_strategy_revision(*, strategy_id: str, old_yaml: str, new_yaml: str, diff: str, rationale: str, conversation_id: int | None = None) -> int` — fills required legacy columns honestly (`rule_id="reflection"`, `ticker=<doc ticker>`… no: ticker comes from parsing new_yaml is Task 3's job; here accept `ticker: str` param), `rule_type="revision"`, `condition=rationale[:200]`, `action="revise strategy"`, `intent=None`, `snapshot=json.dumps({"old_yaml":…,"new_yaml":…,"diff":…,"rationale":…})`, `kind="strategy_revision"`.
  - `ReviewQueue.set_revision_applier(fn: Callable[[str, str], None]) -> None` — mirrors the existing `_executor` injection; `fn(strategy_id, new_yaml)` applies the revision (Task 3 wires it).
  - `ReviewQueue.approve(review_id)` branches on `kind`: `order` → existing path unchanged; `strategy_revision` → same atomic pending→approved claim, then `self._revision_applier(strategy_id, new_yaml)` (missing applier → ReviewError, row stays approved-claim rolled back — claim BEFORE applier call, on applier exception write `execution_result={"error": str(exc)}` and re-raise, mirroring the order path's ExecutionError handling); returns `None` for revisions (route reads kind first).
  - `turns_since` consumers unaffected (reflection turns SHOULD flow into daily consolidation — desirable, no filter change).
- `ConversationStore.start` gains kind; existing callers (chat_service, cli chat, compact tests) keep default `'chat'` — verify with grep, adjust only call sites that must pin a kind.

- [ ] Steps: failing tests (start/latest kind filtering; add_strategy_revision row shape incl. kind + snapshot JSON round-trip; approve on revision kind calls applier with (strategy_id, new_yaml); applier exception recorded + re-raised; reject unchanged for both kinds; legacy rows default kind `order`/`chat` after migration) → red → implement → green → ruff → commit `feat: conversation and review kinds for reflection`.

### Task 3: propose_strategy_revision tool + applier wiring

**Files:**
- Create: `allpath_trade/agent/reflection_tools.py`
- Modify: `allpath_trade/app.py` (wire `queue.set_revision_applier` in `build_components`)
- Test: `tests/test_reflection_tools.py`

**Interfaces:**
- Consumes: `parse_strategy_text(strategy_id, text) -> StrategyDoc` (strategy/loader.py:49); `atomic_write_text(path, text)` (loader.py:22); `StrategyStore.snapshot_version(doc, reason)` (store.py:66); `ReviewQueue.add_strategy_revision` (Task 2).
- Produces:
  - `register_reflection_tools(registry: ToolRegistry, *, strategies: StrategyStore, queue: ReviewQueue) -> None` registering ONE tool `propose_strategy_revision(strategy_id: str, new_yaml: str, rationale: str) -> str`.
  - Tool behaviour: strategy must exist (`_find` via strategies dir file read); `parse_strategy_text(strategy_id, new_yaml)` must pass and parsed `id` must equal `strategy_id` (id changes rejected); diff = `difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=strategy_id+" (current)", tofile=strategy_id+" (proposed)", lineterm="")`; queue via `add_strategy_revision`; returns `"Revision queued for user approval (#<id>). It will not take effect unless approved."`. Validation failure returns the error as the tool result string (agent may retry within its iteration cap) — never raises.
  - `apply_revision(strategies_dir: Path, store: StrategyStore) -> Callable[[str, str], None]` — the applier factory: re-validates (file may have changed since proposal), `atomic_write_text`, `snapshot_version(doc, reason="reflection revision approved via web")`. Wired in `build_components`: `queue.set_revision_applier(apply_revision(settings.strategies_dir, strategy_store))`.
- notify_email preservation: re-validation uses the PROPOSED yaml verbatim — the proposal already carried the full file; a proposal built from a stale base that dropped a later notify_email change is exactly what re-validation cannot catch, so approve overwrites with the proposed text as reviewed by the user (documented in a comment; the diff shown at approval time is regenerated against the CURRENT file by Task 6's route so the user sees the truth).

- [ ] Steps: failing tests (valid proposal queues + returns approval-pending text; id-change rejected; invalid YAML returns error string, queue untouched; applier writes atomically + snapshots + re-validates; applier on now-invalid content raises) → red → implement → green → ruff → commit `feat: propose_strategy_revision tool and revision applier`.

### Task 4: ReflectionSession

**Files:**
- Create: `allpath_trade/reflect.py`
- Modify: `allpath_trade/config.py` (`reflection_max_iters: int = Field(default=12, ge=1)`, `daily_reflection: bool = True`)
- Modify: `.env.example` (two entries with comments)
- Test: `tests/test_reflect.py`

**Interfaces:**
- Consumes: `AgentSession(llm, registry, system_prompt, store=…, conversation_id=…, max_iters=…, compactor=…)` (agent/loop.py:33) and its `run_turn(user_text) -> str`; `build_system_prompt(...)` (agent/context.py:26); `register_readonly_tools` / `register_memory_tools` / `register_reflection_tools`; `ConversationStore.start(kind="reflection")`; `ReportStore` (Task 1); `Compactor` (existing, budget = `context_budget_tokens`); `fence_external`.
- Produces: `class Reflector` with `run_daily(now: datetime | None = None) -> str` (status line for logs) and constructor `Reflector(*, llm, components, report_store, conversations, settings)` — exact dep list refined at implementation, but construction happens in ONE place (Task 5's wiring) so keep it a keyword-only bag.
  - Idempotency: `if report_store.exists(et_date): return "already ran"` — replaces in-memory state for reflection (digest/consolidation keep theirs).
  - Seed briefing (deterministic, each block `fence_external`-wrapped, hard caps: 30 trades, 50 observation lines, all strategies, 2000 chars per block): today's trades incl. `filled_qty`/`filled_avg_price` (NULL rendered as `submitted, fill pending`), today's sentinel observations, positions with day change (via `_cached_quote`-equivalent data source reads, failure → `n/a`), pending queue counts by kind.
  - Prompt contract (REFLECTION_INSTRUCTIONS const): review day vs each strategy's thesis/rules; write lessons via `memory_update` when warranted; propose revisions via `propose_strategy_revision` only when an assumption is measurably off; END with exactly:

```
REPORT
<sections: Day summary / Per-strategy check / Lessons / Proposals>
SUMMARY
<3-5 plain sentences for a phone notification>
```

  - Parsing: split on the LAST line equal to `SUMMARY`; both parts non-empty required. Failure → one corrective `run_turn("Your last message must end with the REPORT/SUMMARY structure. Reproduce it now, nothing else.")` → still bad → `report_store.add(date, body="reflection failed: unparseable report", summary="", …, status="failed")`.
  - `(llm error: …)` / `(stopped: …)` returns from run_turn count as failure/cap: cap-hit text still attempts the corrective turn once (the transcript holds the analysis; the corrective turn extracts it).
  - Tool registry: readonly + memory + reflection ONLY (assert in test: no `place_order`/`draft_strategy`/confirm tools in `registry.specs()` names).
  - tokens_used: sum of turn count proxy is dishonest — record 0 and leave a comment (LLMClient doesn't expose usage; a TODO entry, not a fake number).

- [ ] Steps: failing tests with ScriptedLLM (import from tests.test_agent_loop) driving the real AgentSession: happy path (tool round → REPORT/SUMMARY text → report stored ok, summary extracted); unparseable twice → failed row; idempotent second call; no order tools in registry; briefing contains fenced fill line & pending counts; `daily_reflection=False` handled by caller (Task 5) not here → red → implement → green → ruff → commit `feat: ReflectionSession — bounded full-capability daily reflection`.

### Task 5: Scheduler wiring + notifications

**Files:**
- Modify: `allpath_trade/scheduler.py` (daily(): digest → reflection → consolidation, per-task isolation), `allpath_trade/cli.py` (headless `run` daily_job parity), `allpath_trade/app.py` (build Reflector when LLM configured)
- Modify: `allpath_trade/notify/events.py` (`daily_report(*, date: str, summary: str, body: str) -> tuple[str, str]` — subject `[AllPath] Daily reflection <date>`, body = full report + FOOTER)
- Modify: `allpath_trade/notify/base.py` (`send_report(notifier, subject, summary_body, full_body) -> bool` — type-dispatch: NtfyNotifier gets summary_body, Email/Console get full_body, MultiNotifier recurses children; precedent comment referencing the removed send_test_notification shape)
- Test: `tests/test_scheduler.py`, `tests/test_notify.py`

**Interfaces:**
- Consumes: `Reflector.run_daily()` (Task 4); `_maybe_run_daily(daily_job, state)` (scheduler.py:80) untouched.
- Produces: inside `build_jobs`' `daily()` and the cli `run` daily_job: order digest → `if settings.daily_reflection and reflector is not None: try reflector.run_daily() except print("[reflection] failed: …")` → consolidation. Reflection sends notifications itself? NO — notification send lives in Reflector.run_daily after a successful store (uses `send_report`), keeping scheduler dumb. Push failure never fails the run (bool ignored, one stderr line).

- [ ] Steps: failing tests (daily order + isolation: reflection raising doesn't stop consolidation, digest raising doesn't stop reflection; ntfy child receives summary while email child receives full body via spy MultiNotifier; daily_reflection=False skips) → red → implement → green → ruff → commit `feat: reflection wired into the daily after-close jobs`.

### Task 6: Reports page + revision cards on Pending

**Files:**
- Create: `allpath_trade/web/routes/reports.py`, `allpath_trade/web/templates/reports.html`, `allpath_trade/web/templates/report_detail.html`
- Modify: `allpath_trade/web/app.py` (include router), `allpath_trade/web/templates/base.html` (nav item Reports), `allpath_trade/web/routes/reviews.py` + `templates/reviews.html` + `_review_card.html` (kind branch), `allpath_trade/web/static/app.css` (report/diff styles)
- Test: `tests/test_web_reports.py`, `tests/test_web_reviews.py`

**Interfaces:**
- Consumes: `ReportStore.list/get` (Task 1); `ConversationStore.history(conversation_id)` (replay); `ReviewQueue` rows with `kind`/`snapshot` (Task 2); `queue.approve/reject` (kind-aware); `nav_context` (dashboard.py:48).
- Produces:
  - `GET /reports` — date-desc list (date, summary first sentence, proposal count badge from pending_reviews where kind=strategy_revision AND ts on that date, failed rows shown with muted `failed` chip).
  - `GET /reports/{date}` — `is_valid_date` gate (`^\d{4}-\d{2}-\d{2}$`), 404-with-page pattern via existing `_not_found` idiom; body rendered escaped in `<pre class="report-body">` (white-space: pre-wrap); proposals of that date with status chips; link `View reasoning` → `GET /reports/{date}/transcript` — read-only replay: loop history, role-labelled bubbles, tool calls as muted one-liners (`→ get_bars(ticker=MU)`), ALL content escaped (autoescape; no innerHTML anywhere, no htmx needed).
  - Pending page: revision cards render rationale + regenerated-diff — route recomputes diff between CURRENT file content and proposed yaml at render time (Task 3 note; stale-base honesty), `<pre class="diff">` with `+`/`-` line tinting via CSS classes assigned server-side by prefix, Approve/Reject post to existing endpoints; route's approve handler branches messaging on kind (`Revision applied to <id>.` vs existing order copy) and redirects back.
  - Nav: Reports link; pending badge unchanged (`len(queue.list())` already counts both kinds).

- [ ] Steps: failing tests (list/detail/transcript render + English-only + escape check with `<b>` in body; date-gate 404; revision card shows diff and approve applies via spy applier then shows note; order cards unaffected) → red → implement → green → ruff → commit `feat: Reports pages and strategy-revision cards on Pending`.

### Task 7: Docs closeout

**Files:**
- Modify: `README.md`, `README.zh-CN.md` (Reflection feature + Phase 6 roadmap row), `docs/TODO.md` (close fill-details entry; add known limitations: tokens_used=0 pending LLM usage plumbing; single fill re-poll; no holiday calendar), `CHANGELOG.md` (Phase 6 entry), `.env.example` (verify Task 4 entries present)
- Test: page sweep already covered; `uv run pytest -q` full.

- [ ] Steps: verify each TODO edit against code → write both READMEs in parallel structure → CHANGELOG → full suite + ruff → commit `docs: Phase 6 reflection loop`.

## Self-Review Notes

- Spec ③ fill details → Task 1; ④ queue/tool/applier → Tasks 2-3; ② session → Task 4; ① scheduling + ⑤ push → Task 5; ⑤ pages + ④ UI → Task 6; docs → Task 7. Spec ⑥ degradation distributed: idempotency (T4), isolation (T5), failed rows visible (T6).
- tokens_used: spec listed the column; LLMClient exposes no usage — column ships, value 0, TODO recorded (T7). Honest deviation, documented.
- Type consistency: `add_strategy_revision(ticker=…)` — Task 3's tool passes `parse`d doc's `position.ticker`; Task 2's signature includes `ticker: str` keyword. Verified matching.
