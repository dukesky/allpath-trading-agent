# Changelog

All notable changes to allpath-trade. Dates are merge dates to `main`.

## Phase 6 — 2026-08-10

- After-close **reflection loop**: a bounded agent session (`REFLECTION_MAX_ITERS`
  tool calls, default 12) reviews each active strategy's thesis and rules
  against the day's trades, prices, and observations, then writes a
  REPORT/SUMMARY response — the report for the Reports page, the summary
  for a phone push notification.
- Reflection is advisory only: it has no order tool and cannot write a
  strategy file directly. Durable conclusions go through `memory_update`;
  strategy changes go through a new `propose_strategy_revision` tool that
  queues a diffed revision on the Pending page. Approval is gated by a
  byte-exact staleness check against the file at proposal time and strict
  version-monotonicity — a moving or already-applied base is refused, not
  silently reapplied.
- Trade journal now records fill detail (`filled_qty`, `filled_avg_price`)
  so a reflection pass (and the Reports/transcript views) can see actual
  execution, not just the submitted intent.
- Scheduler runs the after-close jobs in order — daily digest → reflection
  → memory consolidation — each isolated in its own try/except so one
  failing step never blocks the next, and consolidation picks up the same
  night's reflection conclusions.
- Notifications split by channel on the reflection report: ntfy gets the
  short summary as a push banner, email gets the full report body. A
  failed reflection run never sends a notification.
- New **Reports** page: one row per day the reflection job ran, a detail
  view with the full report and that day's proposed revisions, and a
  read-only transcript replay of every tool call the session made. Pending
  now also renders strategy-revision cards with a regenerated diff and a
  stale-base warning when the underlying file changed since the proposal.

## Phase 5.5.3 — 2026-08-10

- Settings: per-section **Test** buttons for Email and Push — test the values as
  typed, nothing persists until the single global **Save** (the combined
  save-and-test flow is gone).
- Settings: `?` help toggles with full setup instructions (Gmail app
  passwords, ntfy).
- Gmail app-password normalization on save: grouping separators are stripped,
  including U+00A0 no-break spaces that some copy paths produce.
- Fixed: notification test endpoints post only their own section's fields;
  blocking SMTP sends moved off the event loop; testing email without a
  recipient now says so instead of failing generically.

## Phase 5.5.2 — 2026-08-09

- Dashboard strategy cards rebuilt: realtime price with signed day change
  (red/green from the real previous close), weight-vs-target progress bar,
  key levels as meaning-tinted chips (add zone / stop / target).
- Whole-app visual pass: light-theme surface contrast, typography scale,
  visible memory tabs, focus states.
- Chat: the input clears the moment a message is sent (echo + thinking
  indicator already show it in the transcript); failed sends restore the text.
- Static assets carry a content-hash `?v=` — stale-CSS caching can no longer
  hide UI changes.
- The bundled example strategy moved to `docs/examples/` so it no longer
  renders as a live (duplicate) strategy.

## Phase 5.5 — 2026-08-09

- Sticky navigation; compact dashboard strategy summary cards.
- Chat: instant echo, "Agent is thinking…" indicator, double-send protection.
- Strategies: lifecycle badges (status / N triggered / pending review) and a
  per-strategy notification toggle (gates email and push, never dispositions).
- Memory page: tabs per layer (profile / strategy / stocks / lessons / changes).
- Settings: model dropdowns fed by a cached OpenRouter catalog with fallback,
  plus a Custom slug option.
- Sentinel heartbeat on the dashboard ("last check Nm ago", staleness warning,
  market-closed state).
- ntfy push notification channel alongside email (`NTFY_URL`).
- Daily memory consolidation now reads the day's web conversations.

## Phase 5 — 2026-08-02

- Local web UI (`allpath-trade serve`): dashboard, chat, pending reviews,
  strategies, memory, settings; token login.
- Queue-backed order confirmation shared between web and sentinel.
- Rolling context compaction for long conversations.
- Thread-safe database access; email notification events; docs.

## Phase 4 — 2026-08-02

- Persistent memory: curated layers (profile / strategy / per-stock / lessons),
  injection guard, observations journal, FTS5 session search, nightly
  consolidation with a dedicated memory-tier model.

## Phase 3 — 2026-08-01

- LLM agent core: provider clients (OpenRouter / OpenAI / Anthropic), tool
  loop, chat REPL, order confirmation tools, sentinel ReviewAgent.

## Phase 2 — 2026-07-31

- Strategy engine: YAML strategies, condition evaluator, rule states, review
  queue, notification layer, hourly sentinel scheduler.

## Phase 1 — 2026-07-30

- Execution foundation: Alpaca (paper) broker adapter, yfinance data layer,
  risk gate, SQLite trade journal, order executor, CLI.
