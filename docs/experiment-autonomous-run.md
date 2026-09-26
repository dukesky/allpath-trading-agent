# Two-week autonomous run — operator runbook

Operator checklist for running the agent on the `paper` account for
~two weeks (≈10 trading days) with zero human intervention, per
`docs/superpowers/specs/2026-08-26-two-week-autonomous-run-design.md`.
This is a temporary experiment posture, not a permanent mode — every step
below has a matching wrap-up step that reverses it.

## 1. Pre-flight

Do these before day one, in order:

```bash
git tag experiment-start
cp allpath-trade.db allpath-trade.db.experiment-start-backup
# also copy any allpath-trade.db-wal / allpath-trade.db-shm if present
cp -r memory memory.experiment-start-backup
cp -r strategies strategies.experiment-start-backup
```

Then, in the Alpaca dashboard, reset the paper account's cash balance to
$100,000 so the run starts from a clean baseline.

**lablab.ai Alpaca hackathon note**: if this run is for the Alpaca AI
Trading Agents Hackathon, use a **brand-new, dedicated paper account**
created specifically for the submission — a reused or reset existing
account is ineligible for judging. On that new account, check the Alpaca
dashboard and confirm its **Options Trading Level is ≥ 2** before starting
the run if you plan to enable `OPTIONS_TRADING` (see below); a level-0/1
account will reject option orders outright.

## 2. `.env` for the run

Set these three for the duration of the experiment:

```bash
EXPERIMENT_AUTO_APPLY_REVISIONS=true
SENTINEL_INTERVAL_MINUTES=30
DRAWDOWN_HALT_PCT=0.15
```

For the hackathon run, also add:

```bash
OPTIONS_TRADING=true
```

This turns on single-leg options (`buy_call`/`buy_put`/`close_options`
rule actions, routed through Alpaca's MCP server) — required since the
hackathon expects options in every strategy. Default is `false`; omit the
line entirely for a non-hackathon run that shouldn't trade options.

`EXPERIMENT_AUTO_APPLY_REVISIONS` lets nightly reflection's own
strategy-revision proposals auto-apply through the normal guarded applier
(byte-exact staleness check, strict version increase, and the
`authorization`/`status` freeze all still apply — see CHANGELOG). It
defaults to `false` and is `.env`-only by design; do not enable it outside
this experiment. `DRAWDOWN_HALT_PCT` at its default (`0.15`) is fine to
leave as-is — it's listed here as a reminder to check it, not because this
run needs a non-default value.

## 3. Keep-alive

The scheduler (sentinel + nightly reflection) only runs while `serve` is
up, so the Mac must not sleep for the duration of the run. Either:

```bash
caffeinate -s uv run allpath-trade serve
```

or a launchd job with `KeepAlive` so it restarts itself if it ever exits.
Minimal example (adjust `WorkingDirectory` and the `uv`/`allpath-trade`
paths for your machine):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.allpath.experiment-run</string>
    <key>WorkingDirectory</key>
    <string>/path/to/allpath-trading-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/uv</string>
        <string>run</string>
        <string>allpath-trade</string>
        <string>serve</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/allpath-experiment.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/allpath-experiment.err</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.allpath.experiment-run.plist`.

## 4. Experiment mode paragraph for `IDENTITY.md`

Append this paragraph to `IDENTITY.md` for the duration of the run. It is
copy-ready — paste it verbatim, do not paraphrase:

```
**Experiment mode (temporary):** you are running a two-week autonomous
validation on the paper account. Be more active than a typical
mid/long-term posture: every nightly reflection MUST review each rule
that triggered (burned) that day and either re-arm it at a price level
you re-justify, or rewrite it. Do not leave a strategy with no armed
rules overnight without stating why in the report.
```

`IDENTITY.md` is read-only to the agent (see the file's own "Authorization
boundary" section), which is exactly why this instruction lives there
rather than only in the kickoff chat — it survives every reflection
session and every restart of `serve` for the life of the run.

## 5. Kickoff-chat checklist

One conversation, day one, before stepping away. Give the agent:

- Budget: $100,000 (matches the reset paper account).
- Risk appetite (your own words — conservative/moderate/aggressive, and
  what that means to you).
- A request for 5–8 tickers — let the agent propose and justify them
  rather than dictating the list.
- An explicit ask for `authorization: auto` on every strategy it drafts
  for this run (the default review workflow demotes to `confirm`/`notify`
  otherwise, which would defeat the zero-intervention design).

Then:

1. Batch-approve the drafted strategies (Reviews page or CLI).
2. Open the Strategies page and verify every strategy for the run shows
   **ACTIVE** status and **auto** authorization before you step away. A
   strategy left at `draft` or `confirm` will not participate in the
   experiment and won't be flagged as an error anywhere — check by eye.

## 6. During the run

- Notifications (email/ntfy/Telegram) are receive-only for the duration
  of the run. Do not act on them except in a genuine emergency.
- If you do manually pause a strategy, that ends the zero-intervention
  claim for that specific strategy — note the pause (what, when, why) so
  it's accounted for in the eventual report. Other strategies are
  unaffected.
- Check the circuit breaker any time with:

  ```bash
  allpath-trade breaker status
  ```

  If it reports `TRIPPED`, every `auto` strategy on that account has
  already been demoted to `confirm` and an alert has already gone out on
  every configured channel — the breaker acts before you check, this
  command only reports what already happened. `allpath-trade breaker
  reset` clears the breaker's own peak/tripped bookkeeping only; it does
  **not** restore any strategy to `auto`. If you decide to resume
  auto-trading after reviewing the account, restoring `auto` is a
  separate, deliberate action you take through the agent (or by
  hand-editing the strategy YAML) — never automatic, on purpose, so a
  drawdown can't silently re-trigger the strategies that caused it.

## 7. Wrap-up

When the run ends (or you decide to end it early):

```bash
git tag experiment-end
cp allpath-trade.db allpath-trade.db.experiment-end-backup
# also copy any allpath-trade.db-wal / allpath-trade.db-shm if present
```

Then:

1. Remove the "Experiment mode (temporary)" paragraph from `IDENTITY.md`
   (step 4 above) — the agent goes back to its normal mid/long-term
   posture.
2. Flip the `.env` flags from step 2 back: `EXPERIMENT_AUTO_APPLY_REVISIONS=false`
   (or remove the line — `false` is the default), restore
   `SENTINEL_INTERVAL_MINUTES` to its normal cadence (default `60`).
   `DRAWDOWN_HALT_PCT` can stay at its default — it's a permanent
   capability, not experiment-only.
3. Building the actual experiment report (equity curve, trade log,
   revision history, token cost) is a separate follow-up task, not part
   of this runbook.

## 8. Human-verify baseline (2026-09-14)

As of this date, the paper-account experiment strategies should use
`authorization: confirm` to keep option trades under human review:

- **All option trades queue for approval**: `buy_call`, `buy_put`, and
  `close_options` rules on `confirm` strategies queue as pending reviews
  instead of auto-executing. Only OPTION approvals are gated by market
  hours (Mon–Fri 09:30–16:00 ET, no US holiday calendar) — attempting to
  approve one outside those windows leaves the item pending rather than
  failing it. The approve-by-link (the one-click link in an email/push
  notification) expires 24h after the review was queued; the Telegram
  Approve/Reject buttons and the in-app Pending page carry no such
  expiry and stay usable until the item is resolved, regardless of
  market hours.
- **Two safety exceptions remain automatic** (documented for the
  research paper):
  - DTE≤1 expiry sweep: positions within one calendar day of expiry are
    sold to close every sentinel tick, regardless of strategy
    authorization, so a bad expiry never ties up capital or drags through
    a weekend.
  - Breaker-tripped closes: once a `confirm` strategy's account's
    drawdown breaker has tripped, `close_options` rules on that strategy
    execute immediately instead of queuing — close-to-safety is never
    delayed.
- **Stock trades follow the normal review flow and are NOT gated by
  market hours**: on a `confirm` strategy, both `hard` and `soft` rules
  queue for approval (soft rules are not skipped — an `auto` strategy's
  own `soft` rules queue too; only `auto` + `hard` executes immediately
  with no review). A queued stock approval, unlike an option approval,
  executes as soon as you approve it, any time of day.
- **Edge cases the paper should also state**:
  - A close approval acts on whatever option positions are actually held
    on the underlying AT APPROVAL TIME — every one of them, including a
    position opened after the rule triggered, not just the one(s) named
    in the original trigger snapshot.
  - An approved `confirm` option buy can still execute while the
    drawdown breaker is tripped (the breaker only forces `close_options`
    to bypass the queue — see above; it does not block a buy a human has
    already approved), same as a stock confirm approval.
  - There is no US holiday calendar (see above) — a US market holiday
    reads as an ordinary open session for every market-hours gate in
    this system.

This baseline ensures the agent proposes and executes option positions
with meaningful human oversight while maintaining circuit-breaker
protection and expiry hygiene.

## 9. Phase 3 — high-activity paper testing (from 2026-09-26)

This phase transitions from human-verified baseline (2026-09-14 to 2026-09-25)
to autonomous rule re-arming and higher activity, **on the same paper account
the baseline ran on** — there is no fresh-account step for phase 3. **Do not
pool baseline data with phase 3 data** — the strategy behavior and risk
posture diverge sharply once re-arming fires.

### Environment settings for phase 3

For the duration of phase 3, set these in `.env`:

```bash
EXPERIMENT_AUTO_APPLY_REVISIONS=true
SENTINEL_INTERVAL_MINUTES=15
DRAWDOWN_HALT_PCT=0.15
MAX_ORDER_VALUE=25000
MAX_POSITION_WEIGHT=0.50
MAX_DAILY_TRADES=50
SHADOW_MAX_ORDER_VALUE=30000
SHADOW_MAX_POSITION_WEIGHT=0.40
SHADOW_MAX_DAILY_TRADES=10
OPTIONS_TRADING=true
```

`EXPERIMENT_AUTO_APPLY_REVISIONS` stays `true`, as it already was for the
baseline. `SENTINEL_INTERVAL_MINUTES` moves from the baseline's `30` down to
`15` — a deliberate change, not a return to any prior value; it's part of why
phase 3 and baseline data must not be pooled. `DRAWDOWN_HALT_PCT` is
unchanged.

The risk ceilings are raised for paper's high-activity posture, up from the
code defaults (`MAX_ORDER_VALUE` $5,000, `MAX_POSITION_WEIGHT` 0.25,
`MAX_DAILY_TRADES` 10):
- `MAX_ORDER_VALUE=25000`: individual order size cap.
- `MAX_POSITION_WEIGHT=0.50`: position concentration limit (50% of account
  equity per position).
- `MAX_DAILY_TRADES=50`: maximum trades per day.
- `max_options_weight` (option exposure cap) is **unchanged** at its default
  10% — nothing in phase 3 raises it.
- Paper trading is back in `auto` mode for strategies (no `confirm` review
  queue).
- `OPTIONS_TRADING=true` for option rule testing.

The `SHADOW_*` lines pin the shadow account (a local ledger mirroring the
user's real, differently-sized brokerage) to its own, unchanged ceilings.
Leaving a `SHADOW_*` setting unset falls back to the matching bare paper
limit above — with paper's limits raised for phase 3, an unset shadow
override would silently inherit those same raised limits, which is not
appropriate for a real-brokerage mirror. Pin all three explicitly.

### Re-arming rules

Rules on strategies can now specify `rearm` (cooldown interval: `60m`, `2h`,
or a bare integer number of minutes; minimum 15 minutes, maximum 10080
minutes/7 days) and `max_fires_per_day` (default 3, 1–20) to fire repeatedly.
Re-arming buys must cap `position_weight` in their condition — a numeric
bound at or below 1, or the name `target_weight`, on every path to true;
option buys (`buy_call`/`buy_put`) cannot re-arm; stock sells and
`close_options` can.

Example rule:
```yaml
  - {id: dip-buy, type: hard, condition: "price < 480 and position_weight < 0.30",
     action: "buy $10000", rearm: 60m, max_fires_per_day: 3}
```

The re-arm check runs on **every sentinel pass** during US market hours
(Mon–Fri 09:30–16:00 ET, no US holiday exceptions) — it is not a once-per-day
step — for each `triggered` rule whose cooldown has elapsed (a 60-second
grace keeps ordinary tick jitter from silently doubling the effective
cooldown) and whose fire count today is still under its daily cap. A rule
re-arms only from state `triggered`, never from `disabled`: setting
`state: disabled` on a rule in its strategy file is the off switch, and it
wins over any stale `triggered` row left in the database from before the
rule was disabled.

Every rule FIRE (re-arming or not) is logged to the `rule_fires` table —
`account`, `strategy_id`, `rule_id`, `ts` only, no trigger price or position
snapshot. Re-arming a rule (the state flip back to armed) writes no
`rule_fires` row by itself, only a `sentinel_rearm` observation.

### Key differences from baseline

1. **Strategy authorization**: revert strategies to `authorization: auto`
   (phase 3 is unattended).
2. **Activity level**: with re-arming, each strategy can now fire multiple
   times per day, increasing position turnover and token usage.
3. **Risk monitoring**: drawdown breaker remains at 15%, but max position
   weight is higher (0.50 vs the code default 0.25) to allow for the
   intended high-activity posture.
4. **Sentinel cadence**: 15-minute ticks, down from the baseline's 30
   minutes.
5. **Data segregation**: phase 3 runs on the existing paper account, not a
   fresh one — separate baseline from phase-3 data by timestamp/tag instead
   (e.g. the `experiment-end`/a new phase-3 tag from section 7).

### Operational notes

- Do not run `allpath-trade check` manually during market hours in phase 3
  (it can race the scheduler's pass and double-fire a re-arming rule).
- A rule that fired before this feature and later gets `rearm` added (for
  example by an auto-applied reflection revision) re-arms on the first
  market tick, because no fire was recorded for it before then.
