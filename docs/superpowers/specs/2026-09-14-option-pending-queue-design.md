# Option Pending-Queue Support — Design

**Date:** 2026-09-14
**Status:** Approved (design session 2026-09-14)
**Context:** The paper account was switched to human-verify (`authorization: confirm`)
as the baseline condition of a two-week research experiment. Option actions on
confirm strategies currently cannot wait for approval: buys are skipped and
`close_options` auto-executes. This feature makes every option trade on a confirm
strategy wait for the user, like stock trades.

## Decisions (user-approved)

1. **Approach A — a separate review kind `option_order`.** The stock `order` path is
   untouched (the experiment depends on it). Rejected: B (polymorphic intent on
   `order` — every existing `order` consumer assumes stock fields) and C (store only
   the rule, re-run at approval — the human would approve a description, not a
   contract; weakest audit trail).
2. **Buys and closes on confirm strategies both queue.**
3. **Exception:** if the account's drawdown breaker has tripped, a confirm strategy's
   `close_options` still executes immediately (a halt must never strand an exit).
4. **The DTE≤1 expiry sweep stays automatic** (documented in the paper as a safety
   exception).
5. **Re-pick at approval:** buys choose a contract again with a live quote, and the
   risk gate runs again, at approval time.
6. Nightly reflection revisions stay auto-apply; the ReviewAgent parser bug is not
   fixed in this feature.

## Prerequisite already shipped

`f8f9756` — option actions may be **authored** on `auto` or `confirm` strategies
(still hard rules only, never `notify`).

## 1. Data model

A queued option trade is a `pending_reviews` row with `kind="option_order"`.
`kind` has no CHECK constraint, so **no migration**.

| Column | Value |
|---|---|
| `ticker` | the underlying (e.g. `NVDA`) |
| `action` | the rule's action text (e.g. `buy_call $1000 dte>=30 otm=5%`) |
| `rule_type`, `condition`, `strategy_id`, `rule_id` | as for stock rows |
| `source` | `sentinel` |
| `intent` | JSON **option instruction** (below), not a fixed contract |
| `snapshot` | trigger-time context plus a `preview` |
| approval token | issued exactly as for `order` rows |

Option instruction (new pydantic model `OptionInstruction` in `broker/base.py`):

- `op`: `"buy"` or `"close"`
- `underlying`: str
- buy only: `right` (`call`/`put`), `min_dte` (int), `otm_pct` (Decimal), `budget`
  (Decimal), `spot_at_trigger` (Decimal)
- close only: `positions_at_trigger`: list of `{occ_symbol, qty}`
- `reason`, `strategy_id`

`snapshot.preview` for a buy: the `OptionPick` chosen at trigger time
(`occ_symbol, expiry, strike, ask, qty, est_premium`). For a close: the same
`positions_at_trigger` list.

New queue method: `ReviewQueue.add_option_order(*, strategy_id, rule_id, ticker,
rule_type, condition, action, snapshot, instruction) -> ReviewHandle`.

## 2. Trigger time (`sentinel.py`)

| Case | Today | After |
|---|---|---|
| confirm · `buy_call`/`buy_put` | skipped | pick a preview contract → queue |
| confirm · `close_options` | executes | queue; **executes if breaker tripped** |
| auto · any option action | executes | unchanged |
| DTE≤1 expiry sweep | automatic | unchanged |
| closed market | rule skipped, stays armed | unchanged |

Details:
- Buy on confirm: `pick_contract` at trigger time. `None` → skipped ("no affordable
  option contract") exactly as today; `OptionsBackendError` → error exactly as
  today. Otherwise queue with the pick as preview.
- Close on confirm: no matching option positions → skipped as today. Breaker
  tripped (`self.breaker is not None and self.breaker.tripped_at()`) → existing
  `_dispatch_close_options` path. Otherwise queue with the current positions.
- Queued option rows go through the same `_agent_review` analysis and
  `_notify_queued` notification path as queued stock rows. The ReviewAgent reads
  only `ticker/condition/action/snapshot`, so it needs no change.
- The rule burns (one-shot) when it fires, as it does today.

## 3. Approval time (`ReviewQueue._approve_option_order`)

`approve()` gains an explicit `option_order` branch in its kind allow-list.

1. Load the row; must be `pending`; parse `OptionInstruction` **before** claiming.
   Corrupt instruction → `ReviewError`, row stays pending.
2. **Market closed → `ReviewError`, row stays pending** (not claimed).
3. No executor or no `options_backend` → `ReviewError`, row stays pending.
4. Atomically claim (same UPDATE as `_approve_order`, clearing the token columns).
5. Execute:
   - **buy:** live spot from `executor.data.get_quote(underlying)`;
     `executor.options_backend.pick_contract(underlying, right, min_dte, otm_pct,
     budget, spot)`. `None` → no order, outcome recorded as "no affordable contract
     at approval". Otherwise build an `OptionIntent` (side BUY) and call
     `executor.execute_option` (the risk gate runs inside).
   - **close:** re-read `executor.broker.get_positions()`; close every OCC position
     whose root is the underlying with `execute_option` (side SELL, `est_premium=0`).
     None left → outcome "nothing to close". One failing position does not stop
     the rest.
   - A quote/backend failure after claiming raises `ExecutionError` (claimed, failed),
     matching the stock path's semantics.
6. Write `execution_result` as JSON with `op`, `preview`, and per-order results —
   preview vs actual is the paper's audit trail.
7. Return `OptionApprovalResult` (new model): `submitted: bool` (at least one order
   submitted), `summary: str` (one human line), `reasons: list[str]`,
   `results: list[ExecutionResult]`.

### Shared market-hours helper

New `allpath_trade/market_hours.py` with `is_us_market_open(now=None) -> bool`
(Mon–Fri 09:30–16:00 America/New_York, no holiday calendar — same known limitation
as today). `scheduler.is_market_hours` and `sentinel._us_market_open_now` delegate to
it, so behavior is unchanged; `ReviewQueue` imports it (the store layer must not
import `sentinel`).

## 4. Surfaces

Each of the four approval surfaces gets an explicit `option_order` branch after the
existing `strategy_revision` / `shadow_edit` branches, formatting
`OptionApprovalResult`:

- `web/routes/reviews.py` approve handler
- `telegram.py` callback handler
- `web/routes/approve.py` (approve-by-link)
- `cli.py` `reviews approve`

Wording mirrors the stock path: success ("Bought 2× NVDA 2026-10-16 $220C @ $4.65" /
"Closed NVDA261016C00220000 ×2"), risk-gate rejection, nothing-to-do outcomes,
"claimed but execution failed", and "market closed — still pending, approve during
regular hours".

Display:
- `_review_card.html`: an `option_order` block showing the trigger-time preview and
  "re-priced at approval"; `execution_result` rendering for option rows.
- `web/routes/reviews.py` price-context enrichment stays gated on `kind == "order"`.
- `events.review_queued`: when `kind == "option_order"`, add a preview line
  ("Contract at trigger: NVDA 2026-10-16 $220C ×2 ≈ $930, re-priced at approval");
  no "Est. size … shares" line.
- Agent guidance (`OPTIONS_ACTIONS_NOTE`): option actions on a confirm strategy wait
  for approval.

## 5. Out of scope

Multi-leg orders; options on the shadow account; the ReviewAgent parser bug;
expiring stale option rows (re-pricing at approval already absorbs price drift);
a holiday calendar.

## 6. Testing

- `market_hours`: open/closed boundaries and weekends; existing scheduler/sentinel
  tests stay green.
- `ReviewQueue`: add/get round trip; approve buy (re-pick, gate pass/reject, pick
  `None`); approve close (re-read positions, partial failure, nothing left); market
  closed stays pending; corrupt instruction stays pending; missing backend stays
  pending; concurrent claim; `execution_result` contains preview and actual.
- `sentinel`: confirm buy queues with preview; confirm close queues; breaker-tripped
  close executes; auto buy/close unchanged; pick `None` still skips; queued rows get
  analysis and notification.
- Surfaces: each of the four formats success, gate rejection, nothing-to-do,
  execution failure, market closed.
- `events.review_queued` option wording; card template renders an option row.

## 7. Rollout

Develop and test on the Pro → push → `scripts/deploy-to-air.sh`. After deploy,
re-arm the two burned option buy rules (`amd-swing/entry-call`,
`nvda-momentum-swing/entry-call`) only if the user wants them live; both were
triggered before the switch to confirm.
