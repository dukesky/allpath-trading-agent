# Shadow Account (Dual-Active) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two fully parallel account pipelines — `paper` (Alpaca sandbox, real simulated execution) and `shadow` (a local ledger mirroring the user's real brokerage; orders are recorded, the user executes them manually). Both always active: own sentinel, queue, journal, reflection, memory (profile layer shared), strategies, chat context, equity curve. Product intent: two practice grounds — one mirrors the user's real account, one starts from zero — the user learns by comparing decisions across them.

**Architecture:** `account` becomes a first-class dimension. `ACCOUNTS = ("paper", "shadow")`. `build_components` returns per-account bundles sharing one DB/notifier/LLM/profile-memory. All account-scoped tables gain an `account` column (legacy rows backfilled `paper`); memory layers and strategy dirs move under per-account subdirectories (one-time idempotent migration; profile stays shared at the root). Spec: `docs/superpowers/specs/2026-08-19-shadow-account-design.md`.

**Tech Stack:** existing only. No new deps. No real-brokerage credentials anywhere, ever.

## Global Constraints

- `uv run pytest -q` (real exit code) + `uv run ruff check .` clean before every commit.
- **Zero cross-account leakage** — every query on an account-scoped table filters by account; memory/strategy writes land in the owning account's directory (profile excepted by design). The end-of-branch review attacks this as a matrix.
- Approvals bind to the ROW's account, never the current view.
- All existing safety invariants per account: risk gate, human-approval queue, reflection guards, injection fencing, notification failure isolation.
- All UI/notification text English; `[Paper]`/`[Shadow]` subject prefixes on every event.
- Migrations idempotent; a pre-migration file backup of memory/ and strategies/ (timestamped sibling dir) before moving anything.

---

### Task 1: DB account dimension + store scoping

**Files:** `store/db.py` (+`account` cols via `_MIGRATIONS`: trades, pending_reviews, reports, conversations; rule_states table — find its real name/location in strategy store — same treatment; reports UNIQUE becomes (account, date) — SQLite can't alter a UNIQUE constraint: create `reports_v2` with the right key, copy, rename, guarded + idempotent), `store/journal.py`, `store/reviews.py`, `store/reports.py`, `store/conversations.py`, `strategy/store.py` (rule states) — each store constructor gains `account: str`; every SELECT/INSERT/UPDATE filters/stamps it. `store/accounts.py`: `ACCOUNTS = ("paper", "shadow")`, `is_valid_account`.
**Test:** every store file's tests: two-account interleaving (write A, write B, read A sees only A); legacy-table migration backfills paper; reports (account,date) uniqueness allows same date across accounts.
- [ ] TDD; suite; ruff; commit `feat: account dimension across stores`.

### Task 2: File-layer migration — per-account memory + strategies

**Files:** `memory/store.py` (layer paths: profile stays `memory/profile.md` shared; stocks/strategies/lessons under `memory/{account}/…`), `strategy/store.py`/`loader.py` (directory becomes `strategies/{account}/`), new `migrate.py` startup hook in app bootstrap: idempotent move of legacy `memory/{stocks,strategies,lessons}` → `memory/paper/…` and `strategies/*.yaml` → `strategies/paper/`, with timestamped backup dirs first; consolidator/reflection memory wiring takes the account's MemoryStore but the SAME profile path.
**Carried from T1 review:** `strategy_versions` is keyed on strategy_id alone — after the directory split, a same-id strategy in each account would share one version history (same leak class as the rule_states PK T1 fixed). Add `account TEXT NOT NULL DEFAULT 'paper'` (plain ALTER) and scope `snapshot_version`/`versions` by the store's account. Also: add the two migration regression tests T1's review had to hand-probe (rebuild idempotence via double connect(), half-migrated DB), and a loud comment at `_MIGRATIONS` that any future `reports`/`rule_states` column must also be added to the rebuild DDL lists.
**Test:** migration idempotence (run twice), backup created, legacy layout → paper layout, fresh install no-op; MemoryStore(account) writes land per account, profile read/write shared; StrategyStore per account.
- [ ] TDD; suite; ruff; commit `feat: per-account memory layers and strategy dirs (profile shared)`.

### Task 3: ShadowLedger broker

**Files:** `broker/shadow.py` (`ShadowLedger(Broker)`, `name="shadow"`), tables `shadow_positions/shadow_cash/shadow_orders/shadow_equity_daily` in SCHEMA. Full ledger semantics from spec §①: instant fill at current quote via injected DataSource; fractional via notional; oversell/short → REJECTED; no-quote → REJECTED with reason; weighted avg cost on buys; last_price fallback valuation with staleness ts; `get_equity_history` from daily table; every `get_account()` upserts today's point; ledger mutation helpers `set_position/set_cash/remove_position/record_fill(order_id, price)` (recompute that order + position avg) used by Task 6's tools — ledger writes NEVER exposed to the agent directly.
**Test:** pure ledger matrix (buy/sell/fractional/oversell/no-quote/avg-cost/valuation/stale-price/equity points/fill correction math).
- [ ] TDD; suite; ruff; commit `feat: ShadowLedger broker`.

### Task 4: Dual components + dual scheduling

**Files:** `app.py` (`AccountComponents` per account: broker (Alpaca for paper — unchanged construction; ShadowLedger for shadow), journal/queue/reports/conversations/strategies/memory/executor/sentinel/consolidator/reflector per account; shared: conn, notifier, llm+usage, app_state, profile), `scheduler.py` (`_run_sentinel_pass` loops ACCOUNTS with per-account heartbeat keys `sentinel_last_pass:{account}` — keep legacy key mirroring paper for dashboard compat until Task 7 updates it; `run_daily_jobs` loops accounts: digest → reflection (ONLY if that account has ≥1 active strategy — the cost gate) → consolidation, each step isolated per account), `cli.py` run parity, `reflect.py`/`consolidate.py` take their account bundle (briefing says which account + shadow wording).
**Carried from T1 review (CRITICAL — must land in this task, before shadow turns/observations first exist):** (a) `search_index` (FTS5) and `observations` are account-blind; a shadow chat turn would surface in paper's `session_search` — straight into the paper agent's context. Add `account` to both (`search_index`: FTS5 cannot ALTER — rebuild with an UNINDEXED account column; `observations`: plain ALTER) and scope `SessionSearch`/`ObservationLog` by account; consolidation/reflection consume their own account's instances. (b) The consolidator's watermark keys (`consolidator_last_turn_id`, and the observation MARKER) are single global app_state keys — under per-account consolidation, interleaved ids mean whichever account runs first advances the mark past the other's turns, silently never consolidating them. Keys become `...:{account}`; the legacy key's value seeds paper's on first read.
**Test:** dual sentinel pass isolation (paper sentinel raising doesn't stop shadow), per-account heartbeats, nightly loop per account + reflection gate (no active strategies → no LLM call), reports keyed (account,date) both run same night.
- [ ] TDD; suite; ruff; commit `feat: dual-active pipelines`.

### Task 5: Account-aware chat, web switcher, Telegram routing, bound approvals

**Files:** `web/chat_service.py` (per-account instances on app.state), `web/app.py`, new account cookie + `web/account_ctx.py` helper (read cookie → validated account, default paper), ALL web routes filter by current account (dashboard, chat, reviews, reports, memory, strategies — sweep every route file), `base.html` global switcher chip (POST /account/switch, cookie, redirect back), `telegram.py` (`/account` command with inline Paper/Shadow buttons; per-chat account state in app_state; message prefix `[Paper]`/`[Shadow]`; chat text routes to that account's ChatService; review callbacks/approve links resolve by the ROW's account — queue lookup must locate the row across accounts then use its bundle), `routes/approve.py` (row's account decides the queue/executor bundle; page shows account chip).
**Carried from T1 review:** store constructors accept any account string (silent third partition risk) — add `is_valid_account` raises to each store constructor here, when the external boundaries (cookie, /account command, CLI flag) start passing non-literal values; `cli.py`'s raw `SELECT kind FROM pending_reviews WHERE id=?` (`_approve_needs_broker`) gains the account filter when the CLI learns accounts.
**Carried from T4 review:** until this task's row-bound lookup lands, shadow's approve links + Telegram approval buttons resolve through the paper queue (the only queue any pre-T5 caller knows how to reach) — T5 MUST land before the branch merges, otherwise a shadow-account order/edit approval silently resolves against the wrong account's queue/executor. It will land before merge: this plan merges as a single branch at the end, not task-by-task.
**Test:** switcher round-trip + cookie; every page shows only current account's data (interleave fixture); TG /account switching + prefixes; callback on a shadow row while TG is on paper still resolves shadow (row-bound); approve link likewise; strangers unchanged.
- [ ] TDD; suite; ruff; commit `feat: account switcher, per-account chat, row-bound approvals`.

### Task 6: Shadow ledger editing — tools, approvals, CSV

**Files:** `agent/shadow_tools.py` (`register_shadow_tools`: set_position/set_cash/remove_position/record_fill — registered ONLY on the shadow account's registries; web/TG → `pending_reviews` kind=`shadow_edit` (payload before→after JSON, applier = ledger mutation after human approval, same claim/rollback discipline as strategy revisions incl. RevisionValidationError-style pending preservation on stale before-state); terminal → blocking confirm), `store/reviews.py` (+kind allowlist entry + applier injection), Settings Brokerage shadow section (ledger summary, CSV upload → parse+preview → one bulk `shadow_edit` proposal, Reset ledger with confirm), notify copy for shadow_edit.
**Test:** tools queue not write; approval mutates ledger; stale before-state → pending; CSV parse/preview/bulk proposal; reject leaves ledger; terminal confirm path; unknown kind still fails closed; English-only.
- [ ] TDD; suite; ruff; commit `feat: shadow ledger editing via approval pipeline`.

### Task 7: Notification prefixes, shadow wording, UI polish, docs

**Files:** `notify/events.py` (+`account` param on every builder → `[Paper]`/`[Shadow]` subject prefix; shadow order receipts say "place this order in your brokerage now"), `notify/dispatch.py` threading account, sentinel/reflector/digest callers, dashboard (account chip, heartbeat per account, shadow empty-state guidance, "price as of" staleness column), reports/memory pages account-scoped headers, `README.md`+`README.zh-CN.md` (dual-account section + the learning framing: mirror your real account AND practice from zero, compare and learn), CHANGELOG, docs/TODO (cost note: dual reflection ≈ 2× nightly Opus when both accounts have active strategies; known limits: dividends/splits manual, no real-broker APIs).
**Carried from T4 review:** `scheduler.py`'s `DIGEST_LAST_DATE_KEY` is still a single global app_state key (the digest itself is one email covering both accounts, so it was left alone in T4 alongside the consolidator's own per-account watermark fix) — once this task's per-account event prefixing lands, revisit whether the digest gains a per-account send gate too; if it does, the key needs the same `...:{account}` suffix + one-time legacy-key seed for paper that `_turn_marker_key`/`consolidate.py` already established, not a fresh ad-hoc scheme.
**Test:** prefix on every event builder; shadow wording; chip rendering; empty-state; English-only sweep incl. new pages; full suite.
- [ ] TDD; suite; ruff; commit `feat: account-prefixed notifications and dual-account UI polish`.

## Self-Review Notes

- Spec §② → T1+T2; §① → T3; §③ → T4; §④ → T5(chat)+T6(tools); §⑤ → T7+T5(TG); §⑥ → T5(switcher)+T7(dashboard); 串扰矩阵 → every task's two-account tests + end-of-branch adversarial review.
- Cross-task: `ACCOUNTS`/`is_valid_account` (T1) used everywhere; `AccountComponents` (T4) consumed by T5/T6; ledger mutation helpers (T3) are T6's applier.
- Sequencing note: after T1/T2 land, the app runs single-account (paper) on the new schema — each task keeps the suite green; dual behavior activates in T4.
