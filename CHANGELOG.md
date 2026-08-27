# Changelog

All notable changes to allpath-trade. Dates are merge dates to `main`.

## Two-week autonomous run prep — 2026-08-26

- **Experiment auto-apply for reflection revisions**: `EXPERIMENT_AUTO_APPLY_REVISIONS`
  (`.env` only, default `false`) lets nightly reflection's own
  `strategy_revision` proposals approve themselves through the exact same
  guarded applier a human approval uses — byte-exact base-YAML staleness
  check, strict version increase, and the existing freeze (a revision can
  touch `thesis`/`rules` only, never `authorization` or `status`) all still
  apply unchanged. A row that fails any check stays `pending` instead of
  applying, same as today. Scoped to `source="reflection"` rows for the
  account being reflected; chat-sourced strategy drafts and order proposals
  are unaffected and still require human approval regardless of this flag.
- **Drawdown circuit breaker**: a new permanent (not experiment-only)
  per-account kill-switch. `DRAWDOWN_HALT_PCT` (default `0.15`, `0`
  disables) tracks each account's equity peak in `app_state`; once equity
  falls more than that fraction below the peak, the breaker trips **once** —
  every `auto` strategy on that account is demoted to `confirm` and a
  high-priority alert goes out on every configured channel (email/ntfy/
  Telegram). Recovery is manual: `allpath-trade breaker status` reports the
  current peak/tripped state, `allpath-trade breaker reset` clears that
  bookkeeping so the breaker can arm again — it deliberately does not
  restore any strategy to `auto`, since that decision needs a human to have
  actually reviewed the account. The dashboard also shows a banner while a
  tripped account's breaker hasn't been reset.
- **Broker HTTP timeout**: `BROKER_HTTP_TIMEOUT_SECONDS` (default `30`) adds
  a socket-level timeout to the Alpaca client, so a hung broker call now
  fails and surfaces as a per-strategy sentinel error instead of stalling a
  sentinel tick (and the dashboard's broker thread pool) indefinitely.

## Setup wizard + image import — 2026-08-22

- **`serve` starts without Alpaca keys**: a fresh install no longer
  deadlocks on a chicken-and-egg credential requirement — the paper
  account gets an `UnconfiguredBroker` when either Alpaca key is missing,
  and the sentinel/reflection chain skips paper for that pass (one
  scrubbed stderr line) rather than erroring, until the keys are saved.
  Every other command still requires them.
- **First-run `/setup` wizard**: an unconfigured install is walked through
  four steps — an LLM key, then Alpaca paper keys (with "where to get a
  key" steps for each provider and a Test button that checks the
  connection before you move on), then importing your Shadow positions,
  then a closing checklist (Telegram, notifications, first strategy).
  Every step is skippable and none is a dead end.
- **Setup gate + banner**: while an LLM key or a full Alpaca pair is
  missing and the wizard hasn't been dismissed, every authenticated GET
  redirects to `/setup` (POSTs are untouched, so a settings save can't be
  bounced mid-flight); once dismissed, a "Setup incomplete — ... missing ·
  Finish setup" banner follows you around instead. Settings → Access
  carries a permanent "Re-run setup" link either way.
- **Per-account onboarding cards**: Chat's empty state shows
  account-specific guidance above the composer — Shadow: "Tell me what you
  hold" with paste/type/screenshot examples; Paper: three clickable
  prompts that fill and focus the input. The Dashboard's broker-failure
  slot gets the same treatment when paper's broker is unconfigured.
- **Image attachments in web chat**: a 📎 control (pick, paste, or
  drag-drop) attaches PNG/JPEG/WebP images, up to 5 MB each and 4 per
  message, alongside your text. Images ride the one turn they're sent
  with — the model sees them on its first reply of that turn — and are
  never stored: the transcript, the FTS index, and the Telegram mirror all
  keep only a `[image: name, size]` placeholder, and nothing is written to
  the database, a log, or any file the app keeps. (The HTTP layer may still
  spool a large upload to an unlinked temporary file while parsing the
  request; a request over the four-image budget is refused with 413 before
  it is parsed at all.)
- **Vision hint before you send**: when the OpenRouter catalog positively
  says the configured chat model has no image input, the Chat composer says
  so up front. Informational only — an unlisted slug, a curated-list
  provider or an unfetched catalog says nothing rather than warning about a
  model that may well see fine.
- **A fixed reply when the model actually refuses an image**: a provider
  error that says the model can't read images (as opposed to a rate limit
  or an outage that merely names a vision model) is answered with "switch
  CHAT_MODEL to a vision-capable model in Settings, or type the positions
  instead" rather than a raw provider string.
- **Telegram photo import**: a photo or image document sent to the paired
  bot rides the same chat-turn path as text — the caption becomes the
  message, and up to four images sent together as one Telegram album
  become a single turn (an album split across two poll batches becomes
  two turns instead, an accepted limit).
- **Screenshot import for Shadow**: attach a screenshot of your positions
  in Chat and the agent restates the table it read back to you before
  queuing the same `shadow_set_position`/`shadow_set_cash` proposals a
  typed or CSV import would — still a normal human-approved proposal,
  never a direct write.

## Shadow dual-active accounts — 2026-08-21

- **A second, always-on account**: `shadow` runs in parallel with `paper`
  from now on — its own sentinel pass, approval queue, conversation/memory,
  strategies, reflection, and equity curve, sharing one process and one DB.
  Shadow is a local ledger that *mirrors your real brokerage*: no real
  credentials, no real order ever routed — every buy/sell a rule or the
  agent decides on is recorded, and the notification tells you plainly to
  place it yourself. Paper is unchanged (Alpaca sandbox, real simulated
  execution).
- **Account switcher**: a `PAPER`/`SHADOW` chip in the top nav (blue/amber)
  picks which account every page shows — dashboard, chat, pending queue,
  reports, memory, strategies — remembered in a cookie, default paper. A
  small dot flags the account you're *not* looking at when it has
  something waiting for you.
- **Shadow ledger editing**: tell the agent in Chat, or go to Settings →
  Brokerage → Shadow to upload a CSV of your real positions (capped at
  2,000 rows) or reset the ledger — every change is a normal
  human-approved proposal, never a direct write.
- **Telegram `/account`**: switch which account a paired chat talks to
  with inline Paper/Shadow buttons; approval callbacks resolve by the
  ROW's own account, not whichever account the chat happens to be on.
- **Every notification says which account**: every subject line is now
  prefixed `[Paper]`/`[Shadow]` (email, ntfy, and the Telegram push body,
  which never renders a subject line of its own). A shadow order that
  gets recorded says so honestly — *"recorded in your shadow ledger —
  place this order in your brokerage now: BUY 4.5 TSLA @ ~$332.01"* —
  never "submitted", since nothing was routed anywhere; a shadow item
  still waiting for approval adds *"if approved, you'll place it
  yourself."* The daily digest is now one send per account, each gated on
  its own once-per-day watermark, and counts shadow activity as "order(s)
  recorded" rather than "trade(s)".
- **Dashboard polish**: a guidance card on an empty shadow ledger
  ("Import your positions — tell the agent in Chat or upload a CSV in
  Settings"), and a "Price as of" column on shadow's positions table when
  a position's last known price is more than a trading day old or a live
  quote just failed — paper's own live-broker feed never shows this
  column at all.
- **Cost**: reflection runs *per account*, gated on that account having at
  least one active strategy, so an empty shadow ledger costs nothing extra
  — but once both accounts have active strategies, nightly LLM spend is
  roughly double running paper alone (visible on Settings → Usage).
- **Ops hardening from the whole-branch review**: the nightly chain
  (digest → reflection → consolidation) now runs as its own scheduler job
  so a slow reflection never stalls sentinel ticks for either account; a
  per-account wall-clock cap `REFLECTION_DEADLINE_SECONDS` (default 1800,
  `.env` only) forces a session to wrap up and still write its report; a
  digest is only marked sent when the channel actually accepted it, and a
  failed day retries (at most 3 attempts); a failed reflection no longer
  blocks that account's retry; the dashboard heartbeat distinguishes
  "last attempted" from "last successful" check. The legacy
  memory/strategies migration never deletes its backup after moving
  anything, parks colliding files as `*.legacy` instead of discarding them,
  and rewrites relative symlinks so they keep resolving.
- See [Two Accounts](README.md#two-accounts) in the README for the full
  picture, and `docs/superpowers/specs/2026-08-19-shadow-account-design.md`
  / `docs/superpowers/plans/2026-08-19-shadow-dual-active.md` for the
  design and task-by-task implementation record.

## Telegram approvals + LLM usage panel — 2026-08-19

- Queued reviews (sentinel soft-rule triggers, chat order proposals,
  chat/reflection strategy proposals) now reach the paired Telegram chat
  with inline **Approve / Reject** buttons — one tap resolves through the
  exact same kind-aware paths the web uses; the callback is bound to the
  paired chat and user, carries a per-review single-use nonce, and the
  message is edited in place with the outcome. Auto-executed hard rules
  push a receipt to Telegram too. A single `notify_review_queued` now
  fans out email / ntfy / Telegram from every queue site.
- Settings → **Usage** tab: per-tier input/output tokens and an estimated
  cost for the last 7 and 30 days, plus a per-day table. Estimates come
  from a built-in price table (unknown models priced at the table maximum
  and flagged); the daily digest gains a best-effort "estimated LLM cost
  today" line (UTC calendar day).
- Known limitation: the terminal chat's LLM calls are not yet recorded in
  the usage table (see `docs/TODO.md`).

## UI round 3 — 2026-08-17

- Dashboard **equity chart**: a server-rendered SVG line of account equity
  from Alpaca's portfolio history, with Week / Month / YTD / Year range
  tabs, the current equity headline and period change coloured by
  direction (the line follows the same sign as the headline), and a
  "since <date>" caption so a young account's "Year" tab reads honestly.
  Degrades to "No history yet" on any broker failure; readable in both
  themes.
- **Settings tabs**: the seven settings sections are now client-side tabs
  inside the same single form with one Save — typed input in other tabs
  survives switching, a validation error reopens the tab it belongs to,
  the active tab lives in the URL hash (deep-linkable, back/forward aware).
- **Reports date filter**: date picker + Go jumps to that day's report (or
  says there isn't one), plus Today / This week / This month / All chips
  computed on the ET calendar; inverted ranges are swapped, malformed
  dates get a 400 notice, never a 500.

## Chat strategy proposals — 2026-08-17

- `draft_strategy` in web/Telegram chat now queues a proposal instead of
  telling the user to open a terminal: it reuses the Phase 6 reflection
  revision pipeline (`pending_reviews`, kind `strategy_revision`), tagged
  `source="chat"`, for both brand-new strategies and revisions to existing
  ones. The terminal `allpath-trade chat` REPL is unchanged — it still
  confirms inline and saves immediately.
- The applier branches its guards on `source`: a reflection-sourced
  revision still cannot change `authorization` or `status` (spec-mandated
  freeze — reflection only fixes rule logic), while a chat-sourced
  revision can, since it reflects the user's own request. A brand-new
  strategy is recorded with an empty `old_yaml` base and the applier
  requires the file to still be absent at approval time.
- A second chat draft for the same strategy supersedes the earlier
  pending one automatically (marked `superseded` with a resolution note)
  rather than piling up duplicate proposals.
- Pending cards and the confirm page show who proposed the change
  (reflection vs. chat), a "new strategy" badge, and a warning when the
  proposal would flip `authorization` to `auto` or `status` from `active`
  to `draft` — both are consequential, easy-to-miss changes to wave
  through. Approval fires a notification with a kind-specific message and
  an approve link, mirrored to Telegram like any other queued item.
- Known limitation: chat-sourced order proposals (`propose_order` in web
  mode) still don't send a notification when queued — pre-existing, not a
  regression from this change — see `docs/TODO.md`.

## Telegram chat channel — 2026-08-12

- Two-way Telegram chat with the same agent, same conversation, same
  memory as the web Chat page: a long-polling `TelegramPoller`
  (`allpath_trade/telegram.py`, stdlib `urllib` only, no new dependency)
  drives the same `ChatService` instance under `allpath-trade serve`.
- Pairing: `/start <your web token>` in a private chat with your bot
  (created via `@BotFather`) pairs exactly one chat, constant-time token
  check, no reply to a wrong/missing token or a stranger. The pairing
  message is best-effort deleted afterward (it carries the web token); the
  Settings page also tells you to delete it yourself, since delete
  permission isn't guaranteed.
- Full mirroring: every web Chat turn (and every approval/reject receipt)
  pushes to the paired Telegram chat as `You (web): ...` plus the reply;
  Telegram-originated turns don't mirror back (no echo loop). Mirror
  sends are fire-and-forget on their own thread pool — a slow or failed
  Telegram send never adds latency to, or breaks, the web chat turn that
  triggered it.
- Message formatting: a narrow Markdown-to-Telegram-HTML renderer
  (`to_telegram_html`, `web/markdown.py`) maps bold/inline-code/code
  fences/tables to Telegram's `b`/`code`/`pre` tags, escaping everything
  else; long replies split at paragraph boundaries under Telegram's 4096-
  character limit, with a plain-text fallback if a message ever fails to
  parse as HTML.
- Settings page: a Telegram section (bot token as a write-only secret
  field, `?` setup help, pairing status masked to the last 4 characters
  of the chat id, and an Unpair button with the same confirm-dialog
  pattern as other destructive actions).
- Known limitations (see `docs/TODO.md`): the poller only runs under
  `serve`, not the headless `run` daemon; a failed mirror push is not
  retried or replayed; message handling is at-most-once by design (a
  mid-turn crash drops the incoming message rather than risk replaying a
  duplicate order proposal on restart); pairing currently reuses the
  long-lived web token as the one-time pairing secret, with a dedicated
  one-shot pairing code recorded as a future improvement.

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
