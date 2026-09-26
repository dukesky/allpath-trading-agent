# Rule Re-arm (Cooldown) — Design

**Date:** 2026-09-26
**Status:** Approved (user, 2026-09-26)
**Context:** The two-week human-verify baseline (2026-09-14 → 2026-09-25) produced 6
fills in 10 trading days. The main cause is that every rule is one-shot: after a
rule fires it stays `triggered` until a nightly revision re-arms it, so by week two
most entry rules were spent. Phase 3 of the paper-account experiment tests the
agent's execution and planning at higher activity. This feature is the frequency
switch.

Phase 3 settings already applied on the Air (2026-09-26): paper strategies back to
`authorization: auto`; paper limits `MAX_ORDER_VALUE=25000`,
`MAX_POSITION_WEIGHT=0.50`, `MAX_DAILY_TRADES=50` (shadow pinned to its previous
values); `SENTINEL_INTERVAL_MINUTES=15`.

## Decisions

1. **Opt-in per rule.** Two new optional rule fields:
   - `rearm`: cooldown after a fire. YAML accepts `60m`, `2h`, or a bare integer
     (minutes). Stored as integer minutes. Minimum 15 (the sentinel interval).
   - `max_fires_per_day`: fires allowed per US/Eastern trading day. Default 3 when
     `rearm` is set, range 1–20. Setting it without `rearm` is an error.
   A rule without `rearm` keeps today's one-shot behavior. Existing strategy files
   are unaffected.
2. **Re-arm check (sentinel, each pass).** A rule re-arms when all hold: state is
   `triggered` (never `disabled`); it has `rearm`; the US market is open; the last
   recorded fire is at least `rearm` minutes ago (no recorded fire counts as
   elapsed); fires since the start of today (ET) are below `max_fires_per_day`; the
   action is not an option buy. A re-armed rule is evaluated in the same pass, so it
   can fire again on that tick. Each re-arm writes a `sentinel_rearm` observation.
3. **Fire log.** New table `rule_fires (id, account, strategy_id, rule_id, ts)`,
   written every time any rule fires (re-arming or not). It drives the cooldown
   and daily cap, and gives the research paper a complete record of when each rule
   fired. Additive `CREATE TABLE IF NOT EXISTS`, no migration.
4. **Authoring-time safety (draft / propose / applier, `authoring=True`):**
   - `rearm` on `buy_call` / `buy_put` is rejected (v1: each re-fire would pick and
     stack a new contract). `close_options` and stock sells may re-arm.
   - `rearm` on a stock buy (`buy $N`, buy-to-target) requires the condition to cap
     `position_weight` from above on every path to true: an `and` chain needs one
     capping term; an `or` needs every branch capped. Otherwise a rule like
     `price < 400 -> buy $25000` would re-buy every cooldown up to the 50% gate.
   - Plain loading never enforces these (same reason as the option checks: loading
     must stay tolerant). The sentinel still refuses to re-arm option buys at
     runtime.
5. **Agent guidance** (`agent/context.py`) documents the grammar, the 15-minute
   minimum, the daily cap, market-hours-only re-arming, the position-weight cap
   requirement, and the option-buy exclusion.

## Out of scope

Web UI display of cooldown / fire counts; re-arming option buys; changing the
nightly reflection's own re-arm path.

## Testing

Model parsing and validation; the position-weight cap helper; authoring accepts
and rejects; store fire log; sentinel: one-shot unchanged, re-arm after cooldown,
no re-arm inside cooldown, daily cap, cap resets next ET day, no re-arm while the
market is closed, disabled never re-arms, option buy never re-arms at runtime;
guidance text present.
