# Telegram Chat Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two-way Telegram chat with the agent, fully synced with the web chat (same ChatService, same conversation, full web→Telegram mirroring), paired to exactly one chat via the web token.

**Architecture:** A daemon-thread long-poller (`allpath_trade/telegram.py`, stdlib urllib only) drives the SAME ChatService instance the web chat uses. Pairing state and update offset live in the `app_state` KV. Offset advances on receipt (at-most-once — a mid-turn crash drops the message rather than replaying a duplicate order proposal; pinned in spec §②). Spec: `docs/superpowers/specs/2026-08-12-telegram-chat-design.md`.

**Tech Stack:** existing only. Telegram Bot API over stdlib `urllib.request`. No webhook, no public exposure, no new dependencies.

## Global Constraints

- `uv run pytest -q` (real exit code — never piped away) and `uv run ruff check .` (line 100) clean before every commit.
- Tests NEVER touch api.telegram.org — fake transport everywhere.
- Bot token: SECRET_FIELDS discipline (write-only, masked, never logged/echoed).
- Unpaired chats: silently ignored (no reply of any kind).
- Web layer never calls `Executor.execute()`; the Telegram path adds NO write capability beyond what web chat already has.
- All UI/bot-system text English; agent replies follow the user's language (no no-CJK assertion on agent content).

---

### Task 1: Config, pairing storage, shared ChatService

**Files:**
- Modify: `allpath_trade/config.py` (`telegram_bot_token: str = ""`), `.env.example`
- Modify: `allpath_trade/web/routes/settings.py` (SECRET_FIELDS + telegram_bot_token)
- Modify: `allpath_trade/store/app_state.py` (constants `TELEGRAM_CHAT_ID_KEY`, `TELEGRAM_OFFSET_KEY`)
- Modify: `allpath_trade/web/routes/chat.py` + `allpath_trade/web/app.py` — hoist ChatService construction to app startup (`app.state.chat_service = ChatService(holder)`); the chat route's lazy `_service()` returns that instance. The poller (Task 3) gets the same object.
- Test: `tests/test_config.py`, `tests/test_web_settings.py`, `tests/test_web_chat.py`

**Interfaces:**
- Produces: `Settings.telegram_bot_token`; `app.state.chat_service` (single shared instance, created at startup, `holder.rebuild()`-safe — ChatService already re-resolves components via `holder`, verify `_stale()` handles a rebuild and note it); app_state key constants.
- Existing chat behavior byte-identical (all web chat tests stay green untouched).

- [ ] Failing tests: settings secret round-trip + mask for the token field; chat route uses the app.state instance (identity check); constants exist.
- [ ] Implement; suite; ruff; commit `feat: telegram config, pairing keys, shared ChatService`.

### Task 2: Telegram transport + formatting

**Files:**
- Create: `allpath_trade/telegram.py` (transport half: `TelegramAPI` class)
- Modify: `allpath_trade/web/markdown.py` (`to_telegram_html(text) -> str`, `split_for_telegram(html, limit=4096) -> list[str]`)
- Test: `tests/test_telegram_api.py`, `tests/test_web_markdown.py`

**Interfaces:**
- `TelegramAPI(token: str, urlopen=urllib.request.urlopen)`: `get_updates(offset: int, timeout_s: int = 50) -> list[dict]` (socket timeout 55; returns [] on any error after one stderr line); `send_message(chat_id: str, html: str) -> bool` (parse_mode=HTML; on API 400 retry ONCE as plain text with tags stripped via the module's own helper; never raises); `send_typing(chat_id: str) -> None` (best-effort, swallow everything). Injectable `urlopen` for tests.
- `to_telegram_html`: escape-first (same discipline as render_markdown — reuse its escape, never re-derive from raw); `**bold**`→`<b>`, `` `code` ``→`<code>`, fences→`<pre>`, heading lines→`<b>` line, tables/lists→`<pre>` blocks preserving alignment; everything else escaped text. Allowed output tags exactly `{b, code, pre}` — test scans output corpus.
- `split_for_telegram`: splits on blank-line boundaries first, hard-splits a single >4096 block as last resort; never splits inside a `<pre>` block unless the block alone exceeds the limit.

- [ ] Failing tests: hostile corpus (script tags, nested markup) only ever escaped; tag whitelist; splitting edge cases (single huge pre, exact-4096, multi-paragraph); transport: get_updates parses updates + returns [] on HTTP error/timeout/garbage JSON; send_message HTML→plain fallback happens exactly once; token never appears in exception text (URL contains it — scrub stderr lines; test asserts).
- [ ] Implement; suite; ruff; commit `feat: telegram transport and message formatting`.

### Task 3: TelegramPoller

**Files:**
- Modify: `allpath_trade/telegram.py` (poller half: `TelegramPoller` class)
- Test: `tests/test_telegram_poller.py`

**Interfaces:**
- `TelegramPoller(api: TelegramAPI, chat_service, app_state: AppState, web_token: str, stop: threading.Event)` with `run_forever() -> None` and single-step `poll_once() -> None` (tests drive poll_once; run_forever loops it with backoff).
- Behavior per spec §②: offset read from/persisted to app_state IMMEDIATELY on receipt (at-most-once; comment the crash-replay rationale verbatim from spec); `/start <token>` pairing with `hmac.compare_digest` (correct token → store chat id, reply "Paired. This chat now talks to your AllPath agent."; wrong/missing token → NO reply); paired chat text → `send_typing` → `chat_service.send(text, source="telegram")` → reply via `send_message(to_telegram_html(reply))` split as needed; unpaired chats silently dropped (stderr counter line, max one per poll batch); re-pair overwrites; errors in one update never kill the batch; backoff 5s→doubling→60s cap in run_forever, reset on success.
- NOTE: `chat_service.send(source=...)` parameter lands in Task 4 — this task calls `chat_service.send(text)` positionally-compatible via a thin adapter or coordinates with Task 4's signature. To avoid cross-task drift the poller calls `chat_service.send(text, source="telegram")` and Task 4 MUST land before Task 3's tests run against the real ChatService — implementer: use a fake chat_service here (tests do anyway); the real wiring test is Task 5's.

- [ ] Failing tests (fake api + fake chat_service + real AppState on tmp DB): pairing matrix (right/wrong/missing token, stranger silence, re-pair overwrite); offset persisted before chat_service is invoked (assert ordering via call log); reply formatting/splitting called; one bad update doesn't stop the batch; backoff sequence (drive run_forever with a scripted failing api + tiny sleeps monkeypatched).
- [ ] Implement; suite; ruff; commit `feat: telegram poller with at-most-once pairing loop`.

### Task 4: ChatService source tagging + full mirroring

**Files:**
- Modify: `allpath_trade/web/chat_service.py`, `allpath_trade/web/routes/chat.py`
- Test: `tests/test_web_chat.py`, `tests/test_chat_mirror.py` (new)

**Interfaces:**
- `ChatService.send(text: str, source: str = "web") -> str` — appended user message dict gains `"source": source` (presentation-extra key like `kind`, protocol-projection already strips it — verify `_PROTOCOL_KEYS`).
- `ChatService.set_mirror(fn: Callable[[str, str, str], None]) -> None` — poller (Task 5 wiring) registers `fn(source, user_text, reply)`. Called AFTER the turn completes, outside `_turn_lock` if possible (read the lock scope; mirroring must not extend turn latency for the web user — fire-and-forget is the mirror fn's own job, but don't hold the lock around it), for BOTH sources; the mirror fn itself decides direction: web-sourced → push `You (web): ...` + reply to Telegram; telegram-sourced → nothing (the poller already replied in-channel; web sees it via shared conversation naturally).
- `note_resolution` lines (approval receipts) also mirror to Telegram (they're part of the full record the user chose) — same hook, source="web".
- Mirror failures: swallowed inside the mirror fn (Task 5 wraps in try/except + thread-pool submit); ChatService itself never sees them.

- [ ] Failing tests: send tags source; mirror called with (source, text, reply) after turn; telegram-source does not re-mirror (mirror fn receives source and the Task-5 fn is tested there — here assert hook mechanics only); note_resolution triggers mirror; no mirror registered → zero behavior change (all existing chat tests untouched-green).
- [ ] Implement; suite; ruff; commit `feat: chat source tagging and mirror hook`.

### Task 5: serve wiring, settings UI, docs

**Files:**
- Modify: `allpath_trade/web/app.py` (lifespan: build TelegramAPI+Poller when token set; daemon thread; stop Event on shutdown; register the mirror fn — a small `_mirror_to_telegram(source, text, reply)` that thread-pool-submits sends, `You (web): {text}` then reply, both through to_telegram_html/split)
- Modify: `allpath_trade/web/templates/settings.html` + `routes/settings.py` (token secret field under a "Telegram" section with `?` help toggle: BotFather steps, `/start <web_token>` pairing instruction; pairing status line — paired chat id masked to last 4 digits / "Not paired"; Unpair POST button clearing the app_state key)
- Modify: `README.md`, `README.zh-CN.md` (feature section), `CHANGELOG.md`, `docs/TODO.md` (known limitations: serve-only, no mirror replay on failure, at-most-once message semantics)
- Test: `tests/test_web_settings.py`, `tests/test_web_app_telegram.py` (new)

**Interfaces:**
- Consumes everything from Tasks 1-4. Poller thread only when `settings.telegram_bot_token` non-empty; rebuild()/settings-save while running: poller keeps the OLD token until restart (document in the settings hint: "Token changes take effect after restart" — honest, avoids thread lifecycle complexity).
- Mirror direction rule lives here: source=="web" → send both lines; else no-op.

- [ ] Failing tests: no token → no thread started (assert via spy); token → thread started with stop event wired to shutdown; mirror fn direction rule; settings section renders (mask, status paired/unpaired, unpair clears key, English-only); README parity.
- [ ] Implement; full suite; ruff; commit `feat: telegram channel wired into serve + settings + docs`.

## Self-Review Notes

- Spec §① pairing → T1(storage)+T3(flow)+T5(UI). §② poller → T3. §③ formatting → T2. §④ mirroring → T4(hook)+T5(direction fn). §⑤ security → constraints + T2 scrub test + T3 silence tests. §⑥ degradation → T2 (transport never raises), T3 (backoff), T5 (no-token no-op). §⑦ covered per task.
- Cross-task signature: `send(text, source="web")` defined in T4; T3 tests use a fake so order T1→T2→T3→T4→T5 works without forward deps; T5 integration-tests the real pair.
- The token-in-URL scrub (T2) matters: Telegram API URLs embed the token; any logged error string must mask it.
