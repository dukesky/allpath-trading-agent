# TDSC research exports

Offline, read-only tools for the IEEE TDSC paper (plan:
`allpath-paper/notes/tdsc-plan-2026-11.md` §3). Code lives in
`allpath_trade/research/`; the CLI is `allpath-research` (or
`python -m allpath_trade.research`).

**Outputs never go into this repo** (it is public and the CSVs are production
trading data). Write them to the private paper repo, `allpath-paper/data/tdsc/`.
`research/tdsc/out/` is gitignored as a safety net.

The tools never load `.env`, never run the file migration `allpath-trade`
does on start, and open the DB with SQLite `mode=ro` + `query_only`.

## Getting a DB copy (never write the production DB)

On the Air, back up read-only, then copy to the Pro. Plain `scp` of the whole
file stalls over Tailscale at ~3 MB; fetch it in chunks:

```bash
ssh tianzhang@100.119.178.89 'cd ~/Projects/allpath-trading-agent && sqlite3 -readonly allpath-trade.db ".backup /tmp/tdsc-ro-copy.db" && mkdir -p /tmp/tdsc-chunks && cd /tmp/tdsc-chunks && gzip -9 -c /tmp/tdsc-ro-copy.db > db.gz && split -b 300k db.gz part_ && shasum -a 256 db.gz && ls'
```

Then fetch each `part_*` with `ssh ... cat`, `cat part_* > db.gz`, compare the
checksum, `gunzip`, and delete `/tmp/tdsc-*` on the Air. The tool can also run
on the Air directly against the live DB (read-only is safe under WAL).

## R4: regime metrics (`regime-metrics`)

```bash
uv run allpath-research regime-metrics --db <copy.db> \
  --from 2026-08-11 --to 2026-11-30 --out ~/Projects/allpath-paper/data/tdsc/r4-<date>
```

Re-runnable for any ET date range. Writes:

| file | grain | content |
|---|---|---|
| `regime_metrics_daily.csv` | account × ET day (weekdays + any active day) | report status/model/tokens; proposals by source × kind; reflection revisions auto / human-approved / vetoed / superseded / pending; latency median/max; gate rejections; regime-skip notices; chat revision outcomes; later reversals of that day's applied revisions; order reviews and latency; auto-executed orders; filled/rejected/error trades; breaker trips and end-of-day state; active strategies by authorization; model per tier; reflection tokens (after R5); `order_thread_msgs` (human messages on Telegram order threads) |
| `revisions.csv` | one row per `strategy_revision` review | source, status, resolver (auto/human/none), latency, rules added/removed/modified, fields changed, `reversed_by` and what was reversed |
| `holds.csv` | one row per (held revision, reason) | F1 allow-list holds under auto: reason text and `category` |
| `order_threads.csv` | one row per human message on a Telegram order thread | account, ts, review id, card/message ids, user id, `forwarded`, text (the owner's own words: keep out of the public repo). Empty unless `TELEGRAM_ORDER_THREADS` / `SHADOW_TELEGRAM_ORDER_THREADS` was on |
| `orders.csv` | one row per trade, plus order reviews with no trade | path: `auto` (no review), `review_approved`, `review_rejected`, `review_pending`, `gate_rejected`, `error` |
| `llm_daily.csv` | ET day × tier × model × purpose | calls and tokens (system-wide) |
| `llm_failures_daily.csv` | ET day × tier × model × error class | failed calls |
| `summary.md` | window × account | totals and per-reflection-day rates |

Definitions:
- Day = US/Eastern date of the UTC timestamp (same as `reports.date`).
- Windows (ET, inclusive): A 08-11..08-27, B 08-28..10-02, A′ 10-25..11-30,
  B′ 12-01..12-31; other days have an empty `window`. Edit `WINDOWS` in
  `regime_metrics.py` if the regime log changes.
- `auto` revision = an `observations` row `reflection_auto_apply` naming the
  review id as auto-applied; `gate_rejections` = the same source saying
  `NOT auto-applied (<guard error>)`.
- `held` revision (F1, from main 8c7a5d7) = a `reflection_auto_apply`
  observation "revision #N for X held for human: <reasons>. Not auto-applied
  (auto-apply allow-list)", else a non-empty `pending_reviews.hold_reason`.
  A held row is pending until a human decides it, so it can also be
  human-approved or vetoed (daily columns `refl_rev_held`,
  `refl_rev_held_pending`, `refl_rev_held_approved`, `refl_rev_held_vetoed`);
  it is never `auto` and never a gate rejection. Each reason maps to a stable
  `category` (`revisions.hold_category`: retarget_ticker, removes_exit,
  adds_rule, size_increase, loosens_exit, loosens_entry, buys_more, rearm,
  rule_type, flips_side, condition_rewrite, changes_action, disables_rule,
  rearms_rule, frozen_field, notify_off, new_strategy, unparseable,
  other_field, other). New reason strings in `strategy/auto_gate.py` land in
  `other` until a pattern is added.
- Reversal = a later applied revision of the same strategy (any source)
  removes a rule this one added, or restores a rule spec (type, condition,
  action, rearm) or a top-level field this one changed. `state` (armed /
  triggered) is ignored.
- Strategy authorization per day comes from `strategy_versions` content.
- Breaker resets are not in the DB; `KNOWN_BREAKER_RESETS` lists them from the
  regime log. Append each new reset there.
- `llm_usage` has no account column, so token and model columns are
  system-wide, except `reflection_tokens_*` (purpose `reflection:<account>`,
  recorded from R5 on).

## R3: rule outcomes (`rule-outcomes`)

```bash
uv run allpath-research rule-outcomes --db <copy.db> --to 2026-12-19 \
  --sources reflection --out ~/Projects/allpath-paper/data/tdsc/r3-<date>/reflection
```

`--sources` is `reflection` (default), a comma list, or `all`. One row per
(revision, rule, change) in `rule_outcomes.csv`, plus `rule_outcomes_summary.md`.
Outcome window per row (`window_kind`):

- `live_after`: applied add/modify, from apply until the rule's next applied
  change (any source) or the data end;
- `before_removal`: applied removal, from when the rule took its last spec to
  the removal;
- `kept_after_veto`: vetoed modify/remove, from the veto until the kept rule's
  next applied change;
- `none`: vetoed add or pending (see R2).

Columns: `fired`, `n_fires`, first/last fire time, first fire's disposition
(executed / queued / skipped / notified / rejected / error), the trade that
followed the first fire (before the rule's next fire, within 5 days), and for
an applied add the review that later removed it (`later_removed_by[_source]`).
Fires merge `sentinel` observations (from 08-11), `rule_fires` (from 09-28)
and trades naming the rule; records within 30 min of a known fire of the same
rule are one fire (2 min lead allowed: the observation is written after
dispatch).

## R2: counterfactual replay (`replay-rules`)

```bash
uv run allpath-research replay-rules --db <copy.db> --asof 2026-12-22 \
  --bars ~/Projects/allpath-paper/data/tdsc/bars \
  --out ~/Projects/allpath-paper/data/tdsc/r2-<date>
```

Targets: every vetoed proposal's added/modified rules as proposed
(`variant=proposed`), the original rule a veto kept (`kept`), and every
applied removal's rule as it was (`removed`); `--sources all` by default.
Each condition runs through `strategy.conditions` on raw daily bars from the
first trading day after the decision's ET date to `--asof`:
`fire_basis=close` when the close satisfies it, `intraday` when only a price
inside [low, high] does. Position terms are dropped from AND conditions
(`position_terms_relaxed=1`); OR/NOT with position terms is `unevaluable`.
Fill = fire-day close; `px/ret/pnl` at +5 and +20 trading days and at `asof`;
`pnl` signs the return by the action (buy +, sell/close −; options proxied by
the underlying). Directional marks only, not agent P&L.

Determinism: bars come from yfinance (raw, `auto_adjust=False`) once and are
cached per ticker as CSV with the covered range; reruns with the same cache,
`--asof` and DB give identical output. `--offline` forbids fetching. Pick an
`--asof` that is a completed trading day (yfinance returned a NaN close for
the fetch day once; non-finite bars are dropped and never cached).
