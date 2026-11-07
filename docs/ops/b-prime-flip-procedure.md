# B′ flip procedure (paper study, 2026-12-01)

Pre-registered procedure for switching the paper account from the human-gated
regime A′ (2026-10-25 → 2026-11-30) to the autonomous regime B′
(2026-12-01 → 2026-12-31). Source: TDSC plan R6 and §2.6
(`~/Projects/allpath-paper/notes/tdsc-plan-2026-11.md`). Only Tian flips the
regime. No code change is involved.

All commands run **on the Air** (`ssh tianzhang@100.119.178.89`), in
`~/Projects/allpath-trading-agent`. `uv` is not on the non-interactive PATH, so
use the full path:

```bash
ssh tianzhang@100.119.178.89
cd ~/Projects/allpath-trading-agent
alias ap='/opt/homebrew/bin/uv run allpath-trade'
```

Every `sqlite3` query below opens the DB with `-readonly`. Never edit the DB
by hand.

## When

- A′ ends with the **2026-11-30 (Mon) reflection**. The nightly chain starts
  at 16:05 ET = **13:05 PT** and takes up to ~30 min per account. That
  reflection must still run human-gated.
- B′ must be in force before the **2026-12-01 (Tue) market open**, 09:30 ET
  = **06:30 PT**, so that day's orders and that evening's reflection are
  autonomous.
- Recommended window: **12-01, 00:00–06:00 PT** (or 11-30 evening after both
  11-30 reports exist). Check that the 11-30 reports are done first (step 1).

## Step 0 (11-30, optional but recommended): close A′ cleanly

Pending items left at the flip are ambiguous: a revision proposed in A′ but
approved in B′ is still an A′ row (the export assigns rows by proposal time),
yet it would be resolved under B′ conditions. Before the flip, Tian approves
or rejects every pending paper item:

```bash
ap reviews --account paper list
```

Note the count of anything left pending in the regime log entry (step 5).

Decision for Tian before 12-01: **shadow**. `regime set` is paper-only. Shadow
follows `SHADOW_EXPERIMENT_AUTO_APPLY_REVISIONS` if set, else the global
`EXPERIMENT_AUTO_APPLY_REVISIONS`. On 11-06 the Air's `.env` has the global
flag `false` and no shadow override, so after the flip **shadow revisions stay
human-gated in B′**. In B, shadow revisions auto-applied (24 of them). For a
like-for-like B′ on shadow, set `SHADOW_EXPERIMENT_AUTO_APPLY_REVISIONS=true`
in `.env` (back it up first) and restart serve
(`launchctl kickstart -k gui/$(id -u)/com.allpath.experiment-run`). To keep
shadow human-gated, change nothing. Either way, log the choice in the regime
log.

Open hazard to decide before flipping (TDSC plan §2.6, ops handover item 6):
paper positions below spent, un-rearmed one-shot stops (NVDA/TSM/MU/AMD, 54%
of equity on 11-06). Under `auto`, dip-buy rules re-arm and buy without
approval while the stops stay spent. Re-anchor the stops by revision during
A′ or accept and log the hazard. If any stop re-arming code ships during B′,
log its deploy time as a condition change.

## Step 1: `regime show`

```bash
ap regime show
ap breaker status --account paper
sqlite3 -readonly allpath-trade.db \
  "SELECT account, date, status, created_at FROM reports
   WHERE date >= '2026-11-30' ORDER BY id"
```

Expect:
- `Regime (paper): human`, auto-apply `False` (source `regime`), no
  `MISMATCH` lines.
- Both `2026-11-30` reports (paper, shadow) present with status `ok`. If the
  paper one is missing or `failed`, wait for the retry (every 5 min) or note
  the failure; do not flip while the 11-30 reflection is still running.
- Breaker: `ok (peak …)` or `TRIPPED at …`.

## Step 2: reset the paper breaker if tripped

`regime set auto` refuses while the paper drawdown breaker is tripped (it
changes nothing in that case). Pre-registered rule: if tripped on 12-01, Tian
resets and flips the same day; B′ starts on the flip day.

```bash
ap breaker reset --account paper
ap breaker status --account paper     # expect: ok (peak not yet recorded)
```

Write down the `TRIPPED at` timestamp and peak shown before the reset; both
go into the regime log. Reset clears only the peak/tripped state; the flip in
step 3 is what returns strategies to `auto` (including any the breaker
demoted, e.g. `crm-fade-swing` on 11-05).

## Step 3: flip

```bash
ap regime set auto --by Tian --note "B' start (TDSC plan R6)"
```

Expect `Regime set to auto by Tian; flipped N strategies.` followed by the
status block: `Regime (paper): auto`, auto-apply `True` (source `regime`),
authorization counts all `auto` (plus any `notify`), no `MISMATCH` lines.

If it prints the breaker refusal, go back to step 2. If it prints
`could not record the regime switch`, some strategy files are already flipped
but the regime is not: fix the DB problem and re-run the same command (it is
idempotent).

Record the exact event:

```bash
sqlite3 -readonly allpath-trade.db \
  "SELECT id, ts, regime, by, note, strategies_flipped FROM regime_events
   ORDER BY id DESC LIMIT 3"
```

No restart is needed: the regime is read from `app_state` at each use.

## Step 4: verify on the next trading day (12-01, after the evening chain)

Run after ~14:00 PT on the first trading day in B′.

1. Regime still `auto`, breaker state:

   ```bash
   ap regime show
   ap breaker status --account paper
   ```

   A breaker trip during B′ does not end B′ (the refusal only blocks the
   flip; the breaker itself demotes tripped strategies to confirm). Log any
   trip in the regime log with its timestamp.

2. The evening reflection ran and its revisions auto-applied within minutes
   (the auto-apply signature is latency < ~3 min and one
   `reflection_auto_apply` observation per row):

   ```bash
   sqlite3 -readonly allpath-trade.db "
   SELECT date, status, model FROM reports
    WHERE account='paper' AND date='2026-12-01';
   SELECT id, strategy_id, status,
          ROUND((julianday(resolved_ts)-julianday(ts))*1440, 1) AS latency_min
     FROM pending_reviews
    WHERE account='paper' AND kind='strategy_revision' AND source='reflection'
      AND ts >= '2026-12-01' ORDER BY id;
   SELECT ts, substr(text, 1, 120) FROM observations
    WHERE account='paper' AND source='reflection_auto_apply'
      AND ts >= '2026-12-01' ORDER BY id;"
   ```

   Expect every reflection revision `approved` with latency under a few
   minutes, and a matching `auto-applied (experiment mode)` observation. A
   `NOT auto-applied (…)` observation is a gate rejection: the row stays
   pending, which is correct behaviour; record it, do not approve it by hand
   unless Tian decides to (that would be a human intervention in B′).
   A `revision #N for X held for human: <reasons>` observation is a TDSC F1
   allow-list demotion (`strategy/auto_gate.py`): the diff retargeted the
   ticker, removed or loosened an exit, added a rule, raised size, etc. The
   row stays pending with `pending_reviews.hold_reason` set, exactly as under
   the human regime. Count these for the paper
   (`text LIKE '%held for human:%'`); resolving one is a human decision.
   If the reflection proposed nothing, the check carries over to the next
   day with a revision.

3. Orders: hard-rule triggers on paper strategies execute without a pending
   row; soft-rule triggers queue and are resolved by the review agent within
   seconds.

   ```bash
   sqlite3 -readonly allpath-trade.db "
   SELECT id, ts, ticker, side, status, strategy_id FROM trades
    WHERE account='paper' AND ts >= '2026-12-01' ORDER BY id;
   SELECT id, rule_type, status,
          ROUND((julianday(resolved_ts)-julianday(ts))*1440, 1) AS latency_min
     FROM pending_reviews
    WHERE account='paper' AND kind IN ('order','option_order')
      AND ts >= '2026-12-01' ORDER BY id;"
   ```

   A paper order row with `rule_type='hard'` waiting for a human, or any row
   left `pending` for hours, means a strategy is still at `confirm`: re-run
   `ap regime show` and look for `MISMATCH`.

4. LLM health (an outage would silently empty B′):

   ```bash
   sqlite3 -readonly allpath-trade.db "
   SELECT tier, model, COUNT(*) FROM llm_usage
    WHERE ts >= '2026-12-01' GROUP BY 1, 2;
   SELECT tier, error_class, COUNT(*) FROM llm_failures
    WHERE ts >= '2026-12-01' GROUP BY 1, 2;"
   ```

## Step 5: log both events in the regime log

Append to `~/Projects/allpath-paper/data/regime-log.md` (on the Pro):

- regime table: `| 2026-12-01 HH:MM | regime=auto (DB) | auto | Tian | B' start; regime_events id N; flipped N strategies; pending at flip: N |`
- condition table, only if step 2 ran: `| <reset ts UTC> | paper drawdown breaker reset before B' flip (Tian) | was TRIPPED at <ts>, peak <value> |`
- condition table: the step 4 result (`verified <date>: reflection revisions auto-applied, latency X–Y min; orders auto`), or what failed.

Copy the `regime_events` row (step 3 query) into the entry so the log and the
DB can be cross-checked.

## Rollback

If B′ has to stop early (Tian's call only):

```bash
ap regime set human --by Tian --note "B' stopped early: <reason>"
```

Log it the same way. B′ then ends at that timestamp.
