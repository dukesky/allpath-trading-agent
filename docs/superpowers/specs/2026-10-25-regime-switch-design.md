# Regime Switch — Design

**Date:** 2026-10-25
**Status:** Approved (Tian, 2026-10-25, in chat)
**Context:** Paper 2 (`allpath-paper/OUTLINE-v3.md`) alternates monthly regimes on the
paper account. Tian's definition: a regime is **two switches flipped together** —
*human-gated* = nightly reflection revisions need approval AND paper orders need
approval (every active paper strategy `authorization: confirm`); *autonomous* = both
automatic (revisions auto-applied AND `authorization: auto`). Until now the two lived
in different places (`EXPERIMENT_AUTO_APPLY_REVISIONS` in `.env`, read once at
process start; `authorization` in each strategy file), were flipped by hand from
several sessions, and drifted out of sync four times (regime log, 2026-09-12 to
2026-10-25). This feature makes one command own both.

## Decisions

1. **The regime lives in the database, not `.env`.** `app_state` key
   `regime:{account}` with value `human` or `auto`. It is read at the moment it is
   needed (each nightly reflection, each page render), so a switch takes effect with
   no restart. `EXPERIMENT_AUTO_APPLY_REVISIONS` stays only as the fallback when no
   regime has ever been set for an account.
2. **Event log.** New table `regime_events (id, ts, account, regime, by, note,
   strategies_flipped)`, appended by every switch. It is the ground-truth history the
   paper reads (read-only).
3. **Paper only.** `regime set` is refused for `shadow`: shadow mirrors the user's real
   account, its orders are always advisory, and `auto` there would silently record
   ledger trades the user never placed.
4. **Shadow revisions get their own setting.** New optional setting
   `SHADOW_EXPERIMENT_AUTO_APPLY_REVISIONS` (same pattern as `SHADOW_MAX_*`). When unset,
   shadow inherits `EXPERIMENT_AUTO_APPLY_REVISIONS`.
5. **Effective "auto-apply revisions" for an account:** the account's regime if set
   (`auto` → true, `human` → false); otherwise the shadow override for shadow; otherwise
   the global flag. Reflection uses this at run time.
6. **`allpath-trade regime set human|auto --by NAME [--note TEXT]`** (paper):
   - writes `regime:paper`;
   - sets every **active** paper strategy whose authorization is `auto` or `confirm` to
     the regime's value (`confirm` for human, `auto` for auto) through the existing
     narrow writer `StrategyStore.set_authorization` (reason names the regime and
     `--by`). `notify` strategies and non-active strategies are untouched;
   - appends a `regime_events` row (with how many strategies were flipped);
   - prints the new status. `--by` is required.
7. **`allpath-trade regime show [--account A]`** prints: the regime (or "not set" plus
   the fallback in use), effective revision auto-apply, the paper strategies'
   authorization counts, any **mismatch** (an active `auto`/`confirm` strategy that does
   not match the regime — e.g. the drawdown breaker demoted it, or someone hand-edited
   a file), and the last 10 events.
8. **New and revised strategies follow the regime.** When a strategy revision or a new
   strategy is applied on the paper account and a regime is set, a resulting
   `authorization` of `auto`/`confirm` that differs from the regime is corrected right
   after the write through `set_authorization` (reason "follows regime …"). `notify` is
   left alone. Without this, a chat draft with `authorization: auto` would trade
   unattended inside a human-gated month.
9. **Visible state.** Every page's nav shows a regime chip for the current account
   (paper: "Human-gated" / "Autonomous" / "Regime not set"); the Pending and Reports
   pages show a warning banner when `regime show` would report a mismatch.

## Out of scope

Scheduling flips automatically (only Tian flips, on the dates in the regime log);
any change to shadow's order authorization; the drawdown breaker's own behaviour (it
may still demote auto→confirm in an autonomous month — that now shows as a mismatch).

## Rollout

Deploy, then run once on the Air:
`allpath-trade regime set human --by Tian --note "A' (started 2026-10-25 15:18 via .env flag)"`.
It flips nothing (all active paper strategies are already `confirm`) and records the
anchor event. Then rewrite the `.env` comment to say the paper regime lives in the
database and is changed only with `allpath-trade regime set`.

## Testing

Effective-flag precedence (regime > shadow override > global); set_regime flips only
active auto/confirm paper strategies and logs one event; shadow refused; status
mismatch detection; reflection auto-apply follows the regime with no restart;
applier corrects a chat draft's authorization to the regime; nav chip and mismatch
banner render.
