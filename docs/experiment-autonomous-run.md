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
