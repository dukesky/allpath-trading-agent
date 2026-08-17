# Chat Strategy Proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In web/Telegram chat, `draft_strategy` queues the draft as a human-approved proposal (reusing the Phase 6 revision pipeline) instead of telling the user to open a terminal — including brand-new strategies.

**Architecture:** Same `pending_reviews` kind=`strategy_revision` rows, tagged `source="chat"`. The applier branches guards on source: reflection keeps its authorization/status freeze; chat proposals skip only that freeze. New-strategy proposals record `old_yaml=""` and the applier's base check becomes "file must not exist". Spec: `docs/superpowers/specs/2026-08-12-chat-strategy-proposals-design.md`.

**Tech Stack:** existing only. No new deps.

## Global Constraints

- `uv run pytest -q` (real exit code) + `uv run ruff check .` clean before every commit.
- Agent NEVER writes strategy files directly; the applier after human approval is the only write path. Reflection guards unchanged.
- Terminal chat's blocking-confirm behavior byte-identical.
- All UI/notification text English.

---

### Task 1: Queue-side — source-aware revision rows, new-strategy base, supersede

**Files:**
- Modify: `allpath_trade/store/reviews.py` (`add_strategy_revision` gains `source: str = "reflection"`, `is_new: bool` derived from `old_yaml == ""`; new `supersede_pending_chat_revision(strategy_id) -> int | None` marking prior pending chat rows `superseded` with resolution_note; `approve()`'s revision branch passes `source` + `old_yaml` to the applier — extend the applier callable signature to `(strategy_id, new_yaml, expected_base_yaml, source)`)
- Modify: `allpath_trade/store/db.py` only if `superseded` needs no schema (it doesn't — status is free text; verify list()/pending filters treat it as resolved)
- Test: `tests/test_reviews.py`

**Interfaces produced:** `add_strategy_revision(..., source="reflection")`; applier signature `(sid, new_yaml, base_yaml, source)`; `supersede_pending_chat_revision`.

- [x] Failing tests: source persisted; supersede marks exactly the pending chat rows for that id (not reflection rows, not other ids), returns superseded id; applier receives source; `superseded` rows excluded from pending list + badge.
- [x] Implement; suite; ruff; commit `feat: source-aware strategy revision rows`.

### Task 2: Applier — source-branched guards + new-strategy base

**Files:**
- Modify: `allpath_trade/agent/reflection_tools.py` `apply_revision_factory` (or move the applier to `strategy/apply.py` — it now serves two proposers; one line why)
- Test: `tests/test_reflection_tools.py`, `tests/test_strategy_apply.py`

- [x] Failing tests: reflection source → authorization/status change rejected (existing tests stay green); chat source → authorization/status change ALLOWED; chat + auto → applied (the warning is UI, not applier); new-strategy (`base_yaml==""`) → applies only when file absent, raises RevisionValidationError if present; all other guards (id, dir, byte-exact base for existing files, version monotonic, YAML validation) enforced for BOTH sources; snapshot reason names the source (`"chat proposal approved via web"` / existing reflection reason).
- [x] Implement; suite; ruff; commit `feat: source-branched revision applier with new-strategy support`.

### Task 3: draft_strategy queues in web/Telegram mode

**Files:**
- Modify: `allpath_trade/agent/action_tools.py` (web branch: validate → version bump per existing rule → supersede prior pending chat proposal for the id → `queue.add_strategy_revision(source="chat", conversation_id=..., old_yaml=current-or-"", ...)` → return the approval-pending message; needs `queue` + conversation id injected — extend `register_action_tools` kwargs, wire in `web/chat_service.py`); tool description updated (says drafts queue for approval in web/Telegram; terminal confirms inline)
- Modify: `allpath_trade/notify/events.py` (`review_queued` copy for revision/new-strategy kinds) + `allpath_trade/web/order_sink.py` or wherever web-mode queue notifications fire — send the queued notification with the approve link when base_url configured (mirror how orders notify)
- Test: `tests/test_action_tools.py`, `tests/test_web_chat.py`, `tests/test_notify_events.py`

- [x] Failing tests: web mode → row queued (source chat, conversation_id set), NO file write, return text contains "#N" + "approve"; second draft same id → previous superseded, text says so; new strategy → old_yaml ""; terminal mode → unchanged (confirm called, file written on yes); notification sent with kind-specific copy + link when configured; agent tool description mentions queueing.
- [x] Implement; suite; ruff; commit `feat: chat drafts queue as strategy proposals`.

### Task 4: UI — proposer + new-strategy badges, auto warning, confirm page parity

**Files:**
- Modify: `allpath_trade/web/templates/_review_card.html`, `_split_diff.html` labels for new (left column "— (new strategy)"), `approve_confirm.html`, `web/routes/reviews.py` + `routes/approve.py` (context: `proposer` chip "reflection"/"chat", `is_new`, `auth_becomes_auto` flag computed by parsing new_yaml vs base), `app.css` minor
- Modify: `strategy_detail.html` — if a pending chat proposal exists for this strategy, a line "A chat draft is awaiting your approval → Pending"
- Test: `tests/test_web_reviews.py`, `tests/test_web_approve.py`, `tests/test_web_strategies.py`

- [ ] Failing tests: chip text per source; New badge + left-column label; auto warning appears exactly when new authorization is auto and base isn't; superseded rows render as resolved with note; confirm page mirrors all three; English-only; escaping (yaml with `<script>`).
- [ ] Implement; suite; ruff; commit `feat: proposal cards show proposer, new-strategy, and auto warnings`.
- [ ] Carried from Task 1+2 review: a chat proposal that flips an existing strategy `active → draft` deserves the same card warning treatment as an `authorization: auto` flip (both are consequential, easy-to-miss changes a reviewer should be flagged about before approving) -- add a `status_becomes_draft`-style flag alongside `auth_becomes_auto`, computed the same way (parse new_yaml vs base), and show it on both the review card and the confirm page.

### Task 5: Docs + closeout

**Files:** `README.md`, `README.zh-CN.md` (Strategies section: chat drafts now save via approval; terminal note removed/updated; the "run allpath-trade chat in a terminal" hint in `chat.html` footer + strategy pages updated), `CHANGELOG.md`, `docs/TODO.md` (close the Phase 5 leftover "web chat cannot save strategies"), the `chat.html` hint text.

- [ ] Verify every hint string in templates that mentions the terminal; update; English-only sweep; suite; commit `docs: chat strategy proposals`.

## Self-Review Notes

- Spec ① → T1 (source) + T2 (branched guards); ② → T1/T2 (empty base) + T4 (badges); ③ → T1 (supersede) + T3 (tool calls it); ④ → T3 + notify; ⑤ → T3 (version rule per path). Safety invariants: T2 tests pin reflection guards unchanged.
- Cross-task signature: applier `(sid, new_yaml, base_yaml, source)` defined T1, implemented T2, exercised end-to-end T3/T4.
- Telegram needs no code: shared ChatService → same tool → same queue → mirror carries the agent's "queued as #N" reply.
