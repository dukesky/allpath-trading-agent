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
| `regime_metrics_daily.csv` | account × ET day (weekdays + any active day) | report status/model/tokens; proposals by source × kind; reflection revisions auto / human-approved / vetoed / superseded / pending; latency median/max; gate rejections; regime-skip notices; chat revision outcomes; later reversals of that day's applied revisions; order reviews and latency; auto-executed orders; filled/rejected/error trades; breaker trips and end-of-day state; active strategies by authorization; model per tier; reflection tokens (after R5) |
| `revisions.csv` | one row per `strategy_revision` review | source, status, resolver (auto/human/none), latency, rules added/removed/modified, fields changed, `reversed_by` and what was reversed |
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
