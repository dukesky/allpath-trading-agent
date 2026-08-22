# Setup Wizard + Image Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A brand-new install can be fully set up inside the web app (LLM key → Alpaca paper keys → shadow import) via a gated `/setup` wizard, every page nudges toward what is still missing, and users can import real positions by attaching screenshots in web/Telegram chat.

**Architecture:** `serve` learns to start with an `UnconfiguredBroker` for paper. A `setup_missing()` helper drives a GET-only redirect gate, a banner, and a four-step wizard that writes through the existing `SettingsStore`. Images ride a transient `images` key on the current turn's user message, are converted to provider image parts by each LLM client, and are popped in a `finally` so nothing persists. Spec: `docs/superpowers/specs/2026-08-21-setup-wizard-and-image-import-design.md`.

**Tech Stack:** existing only (FastAPI, Jinja2, htmx, python-multipart already present, stdlib urllib for Telegram). No new dependencies.

## Global Constraints

- `uv run pytest -q` (REAL exit code, never piped for the decision) + `uv run ruff check .` clean before every commit. Line length 100.
- All UI / notification / bot copy English (`tests/helpers.py::assert_english_only` on every new template and reply).
- Images add NO write capability: ledger changes only via existing tools → `shadow_edit` approval → applier. Web never calls `Executor.execute()`.
- Images never persisted: not in memory history after the turn, not in `conversations`, FTS, compactor summaries, logs, or the Telegram mirror.
- Image limits: PNG/JPG/WebP by magic bytes, ≤ 5 MB each, ≤ 4 per message.
- Wizard writes only existing `Settings` fields through `SettingsStore`; secrets never echo back (`masks`, no `value=`).
- No trading parameter becomes page-editable.

---

### Task 1: `serve` starts without Alpaca keys (`UnconfiguredBroker`)

**Files:**
- Create: `allpath_trade/broker/unconfigured.py`
- Modify: `allpath_trade/broker/base.py` (add `class BrokerNotConfigured(BrokerError)` next to `BrokerError`), `allpath_trade/app.py::_build_broker` (paper: if either key empty and no override → `UnconfiguredBroker()`), `allpath_trade/cli.py` (`needs_broker` excludes `"serve"`; `serve` path never prints "Missing credentials"), `allpath_trade/sentinel.py::run_once` (catch `BrokerNotConfigured` before the generic except: `report.errors.append("paper broker not configured")` and return), `allpath_trade/scheduler.py` (`_run_sentinel_pass`: when `acc.broker` is `UnconfiguredBroker`, print one stderr line `[sentinel] paper: Alpaca keys not set — skipping` and do NOT write `sentinel_last_ok`; `_run_account_daily`: same skip for digest/reflection of that account, return True so the day completes), `allpath_trade/web/routes/dashboard.py::sentinel_heartbeat_status` (accept `broker_unconfigured: bool` → text "Sentinel: paper broker not configured — finish setup", warn True)
- Test: `tests/test_broker_unconfigured.py`, `tests/test_cli.py`, `tests/test_sentinel.py`, `tests/test_scheduler.py`, `tests/test_web_dashboard.py`

**Interfaces produced:** `UnconfiguredBroker(Broker)` with `name = "unconfigured"`, `is_paper = True`; every abstract method raises `BrokerNotConfigured("Alpaca keys are not set — finish setup")`; `get_equity_history` likewise. `BrokerNotConfigured` importable from `allpath_trade.broker.base`.

- [ ] Failing tests: every method raises `BrokerNotConfigured` with the exact message; `_build_broker("paper", settings_without_keys, ...)` returns `UnconfiguredBroker`, with keys returns `AlpacaBroker` (patch the import); `main(["serve"])` with empty keys does not return 2 (patch `cmd_serve` to record the call); `main(["status"])` still returns 2; sentinel `run_once` on an `UnconfiguredBroker` returns a report with the single error and no strategy evaluation; scheduler pass skips paper, still runs shadow, writes no `sentinel_last_ok:paper`, writes `sentinel_last_ok:shadow`; dashboard heartbeat text; dashboard GET with unconfigured paper broker returns 200 (no 500).
- [ ] Implement; suite; ruff; commit `feat: serve starts without Alpaca keys via UnconfiguredBroker`.

### Task 2: `setup_missing` helper + GET-only redirect gate + banner + Settings link

**Files:**
- Create: `allpath_trade/web/setup_status.py`
- Modify: `allpath_trade/web/auth.py` (after the token check succeeds, the gate), `allpath_trade/web/routes/dashboard.py::nav_context` (add `setup_missing: list[str]`, `setup_banner: bool` = missing and path not starting with `/setup`), `allpath_trade/web/templates/base.html` (banner under nav: `Setup incomplete — {{ setup_missing | join(', ') }} missing · <a href="/setup">Finish setup</a>`), `allpath_trade/web/templates/settings.html` (Access section: `<a href="/setup?step=1">Re-run setup</a>`), `allpath_trade/web/static/app.css` (`.setup-banner` muted style, both themes)
- Test: `tests/test_setup_status.py`, `tests/test_web_setup_gate.py`, `tests/test_web_settings.py`

**Interfaces produced:**
```python
# allpath_trade/web/setup_status.py
SETUP_DISMISSED_KEY = "setup_dismissed"
def setup_missing(settings: Settings) -> list[str]   # subset of ["LLM key", "Alpaca keys"], in that order
def llm_key_missing(settings: Settings) -> bool       # provider-aware: openrouter→openrouter_api_key, anthropic→anthropic_api_key, openai→openai_api_key
def alpaca_keys_missing(settings: Settings) -> bool
def setup_dismissed(app_state: AppState) -> bool
GATE_EXEMPT_PREFIXES = ("/setup", "/login", "/logout", "/static", "/a/", "/healthz", "/account/switch")
def should_redirect_to_setup(request_method: str, path: str, settings, app_state) -> bool
```
`/setup` route itself is Task 3; until then the gate redirects to a 404 — acceptable inside this task's tests (assert the 302 `Location`), and Task 3 lands the page.

- [ ] Failing tests: `setup_missing` for the four key combinations and each provider; `should_redirect_to_setup` matrix {missing × dismissed × method GET/POST × exempt/non-exempt path}; through `create_app` + TestClient: GET `/` with both keys missing → 302 `/setup`; POST `/chat/send` → not redirected; GET `/a/1` → not redirected; after `app_state.set("setup_dismissed","1")` → 200; banner text present on `/` when dismissed-but-missing and absent on `/setup*` and when nothing missing; `assert_english_only` on the banner; Settings page contains the Re-run link.
- [ ] Implement; suite; ruff; commit `feat: setup gate, banner, and re-run link`.

### Task 3: `/setup` wizard — page, four steps, save/skip/finish

**Files:**
- Create: `allpath_trade/web/routes/setup.py`, `allpath_trade/web/templates/setup.html`, `allpath_trade/web/templates/_alpaca_signup_steps.html`, `allpath_trade/web/templates/_setup_test_result.html`
- Modify: `allpath_trade/web/app.py` (include router), `allpath_trade/web/templates/settings.html` (Brokerage: `<details class="help-toggle">` including `_alpaca_signup_steps.html` above the Alpaca inputs)
- Test: `tests/test_web_setup.py`, `tests/test_web_settings.py`

**Routes produced:**
- `GET /setup?step=N` → renders the step (default = first incomplete: 1 if LLM key missing, else 2 if Alpaca missing, else 3). Never redirects.
- `POST /setup/step/1` form `llm_provider` (openrouter|anthropic), `llm_api_key` (optional; empty = keep). Writes `LLM_PROVIDER` and the provider's key via `holder.store().set(...)`, `holder.rebuild()`, 303 → `/setup?step=2`.
- `POST /setup/step/2` form `alpaca_api_key`, `alpaca_secret_key` (empty = keep). Writes both, rebuild, 303 → `/setup?step=3`.
- `POST /setup/step/3` → 303 `/setup?step=4` (no fields).
- `POST /setup/open-chat` → sets account cookie to `shadow` (reuse `account_ctx`'s cookie setter) and 303 → `/chat?hint=import`.
- `POST /setup/skip` and `POST /setup/finish` → `app_state.set(SETUP_DISMISSED_KEY, "1")`, 303 → `/`.
- `POST /setup/test-llm` (htmx) form `llm_provider`, `llm_api_key` (falls back to stored key when blank) → builds a throwaway client with `build_llm(settings_copy, tier="chat")` and sends `[{"role":"user","content":"Reply with OK."}]`; renders `_setup_test_result.html` with `ok=True, message="OK · {model} replied"` or `ok=False` + sanitized error (class name + first 120 chars, never the key).
- `POST /setup/test-broker` (htmx) form `alpaca_api_key`, `alpaca_secret_key` (fallback to stored) → `AlpacaBroker(key, secret, paper=True).get_account()` → `"Connected · equity $100,000.00"` (use the `money` filter) or sanitized error.
Both test endpoints must run in a threadpool (`async def` + `run_in_threadpool`) like `test_email`, and persist nothing.

**Template content (all English):** progress header with the four names; step 1 radio + key-source cards with the two URLs + one sentence on tiers; step 2 includes `_alpaca_signup_steps.html` (four numbered cards, exact copy from spec §①); step 3 the two-sentence shadow explanation, ledger summary (reuse Settings' `_shadow_ledger_summary`), Open Chat button (form POST `/setup/open-chat`), the embedded CSV form posting to `/settings/shadow/csv-preview` with target `#shadow-csv-result`, Skip; step 4 checklist with links to `/settings#telegram`, `/settings#notifications`, `/chat`, Finish button. Every step has a "Skip for now" form posting `/setup/skip`.

- [ ] Failing tests: GET `/setup` default step per missing set; each step renders its copy, `assert_english_only`; masks shown and no `value=` on secret inputs; POST step 1 writes exactly `LLM_PROVIDER` + the key into the temp `.env` (read with `SettingsStore.get`) and nothing else, then redirects; blank key keeps the stored one; POST step 2 same for Alpaca; skip/finish set the flag and redirect; `/setup` never redirects even when nothing missing; test-llm with a fake `build_llm` (monkeypatch) → OK copy / error copy, `.env` unchanged, response does not contain the typed key; test-broker with a fake `AlpacaBroker` likewise; open-chat sets cookie `account=shadow` and redirects to `/chat?hint=import`; Settings Brokerage contains the signup steps include.
- [ ] Implement; suite; ruff; commit `feat: first-run setup wizard`.

### Task 4: Per-account hints — chat empty state, dashboard cards

**Files:**
- Modify: `allpath_trade/web/routes/chat.py` (`_render`: `onboarding = len(messages) == 0 or request.query_params.get("hint") == "import"`, `hint_import = ...`), `allpath_trade/web/templates/chat.html` (the `onboarding` card above the input: per-account copy + three `<button type="button" class="example" data-fill="...">` that set `#chat-input` value; a 6-line inline script in `app.js` or the existing chat script), `allpath_trade/web/templates/dashboard.html` + `allpath_trade/web/routes/dashboard.py` (when the paper broker is `UnconfiguredBroker` → guidance card `Connect your Alpaca paper account → <a href="/setup?step=2">Setup step 2</a>` in the broker-failure slot; shadow empty-ledger card text gains ", or attach a screenshot in Chat"), `allpath_trade/web/static/app.css`
- Test: `tests/test_web_chat.py`, `tests/test_web_dashboard.py`, `tests/test_web_account_switcher.py`

Copy (exact): Shadow card title *Tell me what you hold*, body *Paste your positions, type them, or attach a screenshot of your brokerage — every change is queued for your approval.* Examples: `I own 10 NVDA at 118.40 and 5,000 cash.` / `Here is my portfolio: AAPL 20 @ 180, MSFT 5 @ 410, cash 12,000.` / `Set my cash to 25,000.` Paper card title *Ask me anything about the market*, body *I can look at a ticker, draft a strategy, or explain what I can do.* Examples: `What do you think of TSLA right now?` / `Draft a strategy that buys NVDA on a 5% dip.` / `What can you do?`

- [ ] Failing tests: empty shadow conversation → shadow card + three examples; empty paper → paper card; non-empty → no card; `?hint=import` with turns → shadow card present; `assert_english_only`; dashboard unconfigured-broker card with the step-2 link; shadow empty card mentions screenshot.
- [ ] Implement; suite; ruff; commit `feat: per-account onboarding hints`.

### Task 5: Image attachments — core (dataclass, `run_turn`, LLM client conversion, error mapping)

**Files:**
- Create: `allpath_trade/agent/attachments.py`
- Modify: `allpath_trade/agent/loop.py` (`run_turn(user_text, extra=None, images=None)`; `_protocol_only` → `_protocol_message` emitting list-content when `images` present; `finally: message.pop("images", None)`), `allpath_trade/llm/anthropic_client.py::_convert` (list content → `image` blocks with `{"type":"base64","media_type":mime,"data":b64}` then the `text` block), `allpath_trade/llm/openai_compat.py` (list content → `[{"type":"image_url","image_url":{"url":"data:<mime>;base64,<b64>"}}, {"type":"text","text":...}]`), `allpath_trade/llm/base.py` (add `class LLMImageUnsupported(LLMError)`; both clients raise it when the provider error message matches `re.compile(r"image|vision|modality|multimodal", re.I)` AND the request carried image parts), `allpath_trade/web/chat_service.py` (`send(text, source="web", images: list[ImageAttachment] | None = None)`; on `LLMImageUnsupported` record the reply `IMAGE_UNSUPPORTED_REPLY` via the session's normal assistant append and return it), `allpath_trade/agent/context.py` (system prompt section "Screenshots of positions" with the spec §③ guidance text)
- Test: `tests/test_attachments.py`, `tests/test_agent_loop.py`, `tests/test_llm_anthropic.py`, `tests/test_llm_openai_compat.py`, `tests/test_web_chat.py`

**Interfaces produced:**
```python
# allpath_trade/agent/attachments.py
MAX_IMAGES = 4; MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_MIMES = ("image/png", "image/jpeg", "image/webp")
@dataclass(frozen=True)
class ImageAttachment:
    data: bytes; mime: str; name: str
    @property
    def size(self) -> int
    def placeholder(self) -> str          # "[image: positions.png, 312 KB]"
def sniff_mime(data: bytes) -> str | None  # magic bytes: PNG 89504E47, JPEG FFD8FF, WebP RIFF....WEBP
class AttachmentError(ValueError)
def validate_images(items: list[tuple[bytes, str]]) -> list[ImageAttachment]
    # raises AttachmentError("Up to 4 images per message.") / ("Image too large (max 5 MB).") / ("Only PNG, JPEG, or WebP images are supported.")
def placeholders(images) -> str          # joined with spaces
IMAGE_UNSUPPORTED_REPLY = ("This model can't read images — switch CHAT_MODEL to a vision-capable "
                           "model in Settings, or type the positions instead.")
```
Unified message shape the clients receive when images are present: `{"role":"user","content":[{"type":"image","mime":..., "data": bytes}, ..., {"type":"text","text": str}]}`. `display` on the stored message = `placeholders(images) + " " + text`.

- [ ] Failing tests: `sniff_mime` on real headers and on a renamed text file → None; `validate_images` each error; `placeholder()` format with KB rounding; `run_turn(..., images=[...])` with ScriptedLLM asserting the first `complete()` call's last user message has list content with an image part then a text part; after `run_turn` returns, `session.history[-2]` has no `images` key and its `content` is the plain text, `display` starts with the placeholder; same after the LLM raises; both client `_convert`s produce provider shapes (assert exact dicts); `LLMImageUnsupported` raised for a provider error containing "image" when images were sent, plain `LLMError` otherwise; `ChatService.send` maps it to `IMAGE_UNSUPPORTED_REPLY` and records the turn; system prompt contains the screenshot guidance.
- [ ] Implement; suite; ruff; commit `feat: image attachments on chat turns (never persisted)`.

### Task 6: Web upload — 📎 button, paste/drag, multipart `/chat/send`, mirror placeholder

**Files:**
- Modify: `allpath_trade/web/routes/chat.py::send` (signature `async def send(request, message: str = Form(""), images: list[UploadFile] = File([]))`; read each with a hard cap `MAX_IMAGE_BYTES + 1` and reject on overflow; `validate_images`; on `AttachmentError` return the chat fragment with `notice=str(exc)` and status 400, no turn; empty text + images → `"Here is an image."`), `allpath_trade/web/templates/chat.html` (`hx-encoding="multipart/form-data"`, 📎 button, hidden file input `accept="image/png,image/jpeg,image/webp" multiple`, thumbnail strip with remove ×, paste/drop handlers, client-side size/count/type check with inline reason; hint when `vision_hint` is false), `allpath_trade/web/templates/_chat_messages.html` (render `display` for user turns — already the case; ensure the placeholder shows), `allpath_trade/web/app.py::_mirror_to_telegram` (user text mirrored = the stored `display`, i.e. placeholder + text), `allpath_trade/web/routes/chat.py::_render` (`vision_hint: bool` from the cached OpenRouter catalog: True unless the entry for `settings.chat_model` exists and lacks `"image"` in `architecture.input_modalities`; informational only), `allpath_trade/web/static/app.js`/`app.css`
- Test: `tests/test_web_chat.py`, `tests/test_chat_mirror.py`

- [ ] Failing tests: multipart POST with 1 PNG → ScriptedLLM receives an image part, response 200, message list shows `[image: a.png, 1 KB] hello`; 5 images → 400 with the count message and no turn recorded; 6 MB → 400 size message; `.png` file with text bytes → 400 type message; images-only → default text; mirror receives the placeholder text, never bytes (assert the fake mirror's arg is a `str` containing `[image:`); `vision_hint` False when catalog entry lacks image modality; template has the 📎 control and `hx-encoding`; `assert_english_only`.
- [ ] Implement; suite; ruff; commit `feat: attach images in web chat`.

### Task 7: Telegram photos and image documents

**Files:**
- Modify: `allpath_trade/telegram.py` (`TelegramAPI.get_file(file_id) -> str | None` (file_path) and `download_file(file_path, max_bytes) -> bytes | None` via `https://api.telegram.org/file/bot<token>/<path>`, reading at most `max_bytes + 1`; the poller's update handler: accept `message.photo` (last/largest `PhotoSize`) or `message.document` with `mime_type` in `ALLOWED_MIMES`; text = `caption` or `""`; pairing/ownership checks identical to text; albums: buffer updates sharing `media_group_id` within one `poll_once` batch, cap 4 with reply `Up to 4 images per message.`; oversize → `Image too large (max 5 MB).`; download failure → `Couldn't download that image — try again.`; then `_handle_chat_text(chat_id, text, images=...)` → `chat_service.send(text, source="telegram", images=images)`), token scrubber already covers the file URL — add a test proving it
- Test: `tests/test_telegram_poller.py`, `tests/test_telegram_api.py`

- [ ] Failing tests: photo update from the paired chat → `get_file` + `download_file` called, `send` receives one `ImageAttachment` with sniffed mime, reply prefixed; document with `image/webp` same; document with `application/pdf` ignored as before; unpaired sender photo → dropped, no download; album of 2 → one `send` with 2 images and caption; album of 5 → reply with the cap message, no send; download returning > 5 MB → size reply; `get_file` failure → download-failure reply; stderr line for a failed download does not contain the bot token; mirror not triggered for telegram-sourced turns (existing policy).
- [ ] Implement; suite; ruff; commit `feat: Telegram photo import into chat`.

### Task 8: Docs + closeout

**Files:** `README.md` (Getting started: "run `serve`, open the URL, the setup wizard walks you through LLM key → Alpaca paper keys → shadow import"; Two Accounts: screenshot import sentence + limits; Telegram: send a photo), `README.zh-CN.md` (same), `CHANGELOG.md` (entry "Setup wizard + image import — <merge date>"), `.env.example` (note that `serve` starts without Alpaca keys and the wizard collects them), `docs/TODO.md` (record: no album support across poll batches; vision hint is OpenRouter-only).

- [ ] Update docs; English-only sweep over new templates; suite; ruff; commit `docs: setup wizard and image import`.

## Self-Review Notes

- Spec ⓪ → T1; ① gate/banner/link → T2, page/steps/tests → T3; ② → T4 (+ Settings help in T3); ③ core → T5, web → T6, Telegram → T7, agent guidance → T5; ④ error table → T3/T5/T6/T7 tests; ⑤ → each task's tests; ⑥ order = task order.
- Cross-task names: `UnconfiguredBroker`/`BrokerNotConfigured` (T1) used by T1 scheduler/dashboard and T4 dashboard card; `setup_missing`/`SETUP_DISMISSED_KEY` (T2) used by T3; `ImageAttachment`/`validate_images`/`placeholders`/`IMAGE_UNSUPPORTED_REPLY` (T5) used by T6/T7; `ChatService.send(..., images=)` (T5) used by T6/T7.
- Invariant pins: T5 asserts no `images` key after a turn (success and error); T6/T7 assert mirrors and records carry only placeholders; no task adds an `Executor` caller or a ledger writer.
