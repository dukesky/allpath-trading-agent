# Two-Week Autonomous Run — Experiment Design

**Date:** 2026-08-26
**Status:** Approved (design session 2026-08-26)

## Goal

Run the agent for ~two weeks (≈10 trading days) on the `paper` account with **zero
human intervention**, to validate end-to-end that the agent can operate a
portfolio autonomously: pick tickers, write strategies, execute triggers,
reflect nightly, revise its own strategies, and avoid disasters. The run's data
(equity curve, trade log, revision history, token cost) becomes material for a
paper draft and a public post.

## Key decisions (user-approved)

1. **Autonomy boundary:** nightly reflection's strategy-revision proposals
   auto-apply during the experiment (env-gated, default OFF). The experiment
   validates a *self-evolving* agent, not just fixed-strategy execution.
2. **Activity:** no mechanical re-arm. The experiment's reflection prompt
   explicitly instructs the agent to review burned rules every night and
   re-arm or rewrite them at new price levels. All re-arm behavior is
   attributable to agent decisions.
3. **Initial portfolio:** the agent drafts it. Day one, the user has a single
   kickoff chat giving budget, risk appetite, and the "more active than
   typical mid/long-term" requirement; the agent picks 5–8 tickers and writes
   the strategies; the user batch-approves once and steps away.
4. **Circuit breaker:** a drawdown kill-switch is added as a *permanent*
   product capability (dormant unless tripped), not an experiment-only toggle.

## What requires no code

- **Auto execution** already exists: `authorization: auto` strategies execute
  hard rules through the risk gate directly and let the ReviewAgent
  auto-approve/reject soft rules (`sentinel.py` `_dispatch`/`_agent_review`).
  The experiment strategies simply use `auto`.
- **Sentinel cadence** is already configurable: `sentinel_interval_minutes`
  (default 60). The experiment sets it to 30 via `.env`.
- **Account:** existing `paper` account, reset in the Alpaca dashboard to
  $100k cash before the run. `shadow` is untouched and unaffected (account
  isolation already exists). No real brokerage is involved anywhere.

## Change A — auto-apply reflection revisions (experiment plugin, default OFF)

New setting `experiment_auto_apply_revisions: bool = False` (`.env` only,
e.g. `EXPERIMENT_AUTO_APPLY_REVISIONS=true`).

When ON, after the nightly reflection queues a `strategy_revision` row, the
reflection chain immediately approves it through the existing
`ReviewQueue`/applier path — the same code path a human approval takes. Every
existing protection stays active and is NOT bypassed:

- byte-exact base-YAML staleness check,
- strict version increase,
- id/existence pre-flight,
- the reflection **freeze**: revisions may touch `thesis` + `rules` only —
  they can never change `authorization` (so a `notify`/`confirm` strategy
  cannot be promoted to `auto` by the agent) or `status`.

If any check fails, the row stays `pending` exactly as today (that night's
revision is simply not applied; the next reflection sees it). The
burned-trigger re-arm warning, which today surfaces in the human approval UI,
is recorded as an observation (`source="reflection_auto_apply"`) on the auto
path so the paper trail survives.

Scope: applies only to `source='reflection'` revision rows created by the
reflection chain itself, for the account being reflected. Chat-sourced
proposals and order proposals still require human approval.

## Change B — drawdown circuit breaker (permanent capability)

New settings: `drawdown_halt_pct` (default e.g. `0.15`; `0` disables) — the
max tolerated drawdown from the account's equity peak.

Mechanism:

- Peak equity per account is tracked in the `app_state` KV, updated at the
  start of every sentinel `run_once` from the freshly fetched account equity.
- If `(peak - equity) / peak > drawdown_halt_pct`: **trip once** —
  - demote every `auto` strategy of that account to `confirm` (YAML write,
    one observation per strategy + one summary observation),
  - send a high-priority alert on every configured channel (ntfy + email +
    Telegram), clearly saying trading is halted pending user review,
  - record the tripped state in `app_state` so subsequent ticks do not
    re-demote or re-alert.
- Recovery is manual: the user flips strategies back to `auto` (talking to
  the agent / editing YAML) and clears the tripped flag (Settings or CLI —
  exact surface decided in the plan). Peak resets when the flag clears.
- The breaker is per-account. Shadow participates in the check (it has
  equity) but its "halt" only demotes authorization — nothing was ever routed
  for shadow anyway.

## Change C — broker/data HTTP timeouts (permanent hardening)

`llm_timeout_seconds` already exists (ops-hardening round). The remaining
unattended-run hazard is documented in docs/TODO.md: the Alpaca
`TradingClient` and the yfinance quote path issue HTTP calls with **no socket
timeout**. One hung call inside the sentinel tick stalls monitoring silently
(APScheduler `max_instances=1` skips subsequent ticks) — unacceptable for two
unattended weeks.

Add explicit request timeouts to:

- `broker/alpaca.py` (`TradingClient` construction / request layer),
- `data/yf.py` quote fetches.

Default ~30s, one shared `.env` setting (name decided in the plan). A timeout
raises the same exception paths the sentinel already isolates per-strategy
(`report.errors` + `sentinel_error` observation), so a flaky network degrades
to a logged skipped tick instead of a hang.

## Experiment protocol (ops, no code)

1. Tag the repo and back up `allpath-trade.db` + `memory/` + `strategies/`
   before day one (baseline for the writeup).
2. Reset the Alpaca paper account to $100k. Restart `serve` under
   `caffeinate -s` (or a launchd KeepAlive job) so the Mac cannot sleep the
   scheduler.
3. `.env` for the run: `EXPERIMENT_AUTO_APPLY_REVISIONS=true`,
   `SENTINEL_INTERVAL_MINUTES=30`, drawdown setting at default 15%.
4. Kickoff chat (the only steering input): budget, risk appetite, "more
   active" requirement, and the nightly re-arm expectation. The re-arm
   expectation also goes into the experiment's reflection guidance so it
   survives beyond one conversation (exact placement — IDENTITY.md vs
   reflection briefing — decided in the plan; IDENTITY.md is read-only to the
   agent, which makes it tamper-proof for the run).
5. During the run the user only *receives* notifications. Manual pause of a
   strategy remains available as an emergency valve; using it ends the
   "zero-intervention" claim for that strategy and is noted in the log.
6. After the run: a summary/export script assembles trades, win rate, max
   drawdown, revision history, and token cost into an experiment report.
   **Not built now** — separate task at the end of the run.

## Out of scope

- No real-money account, no routing capability for shadow.
- No change to the confirm flow itself ("default-approve" hack rejected; the
  `authorization` tier is the correct mechanism).
- No mechanical trigger re-arm, no weekly/monthly report aggregation.
- The end-of-run report script.

## Testing

- Change A: unit tests around the reflection chain — revision auto-applies
  when the flag is on; stays pending when off; each freeze/staleness/version
  failure leaves the row pending; observation written on burned-trigger
  re-arm; chat-sourced rows unaffected.
- Change B: peak tracking across ticks; trip demotes exactly the account's
  `auto` strategies, alerts once, never re-trips; disabled at `0`; per-account
  isolation (paper trip leaves shadow untouched); manual clear resets peak.
- Change C: constructed clients carry the timeout; a simulated timeout
  surfaces as a per-strategy sentinel error, not a hang.
