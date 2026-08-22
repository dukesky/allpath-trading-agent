# Setup wizard, account onboarding hints, and image import in chat

Date: 2026-08-21 · Status: approved in conversation, ready for planning

## Goal

A new user who has just run `allpath-trade serve` must be able to finish
the whole setup — LLM key, Alpaca paper account, shadow ledger import —
inside the web app, without reading the README or the promo site. Once set
up, the app keeps nudging the user toward the next missing thing, per
account. Users can also import their real positions into the shadow ledger
by attaching a brokerage screenshot to a chat message (web or Telegram).

## Decisions (from the design conversation)

| Question | Decision |
|---|---|
| Scope | **B**: first-run wizard (`/setup`) + per-account contextual hints |
| Image import | **A**: chat attachment → vision-capable chat model → existing `shadow_set_position` / `shadow_set_cash` tools → Pending. No separate parser. Telegram photos take the same path. |
| Image retention | **A**: never persisted. The image rides only on the current turn's LLM request; the conversation record keeps a `[image: name, size]` placeholder. |
| Wizard gating | **A**: after login, redirect every HTML page to `/setup` while `setup_dismissed` is unset AND (LLM key missing OR Alpaca keys missing). Every step has Skip; Skip/Finish sets `setup_dismissed`. A banner stays on every page while anything is missing. Settings has a permanent "Re-run setup" link. |
| Image limits | PNG/JPG/WebP, ≤ 5 MB each, ≤ 4 per message |

## Non-goals

- No dedicated "import from screenshot" parser or Settings entry (the chat
  model does the reading; corrections happen in conversation).
- No live (non-paper) Alpaca onboarding — the wizard only explains paper.
- No capability probing of the chat model; an unsupported model surfaces
  as a clear reply, not a pre-check.
- No new dependencies. `python-multipart` is already a dependency, so
  multipart uploads are fine.

---

## ① Setup wizard

### Gating middleware

In `web/auth.py` (after the token check succeeds) a `setup_gate`:

```
missing = setup_missing(settings)            # ["LLM key", "Alpaca keys"] subset
if missing and not app_state.get("setup_dismissed") and is_html_page(request):
    redirect 302 -> /setup
```

- `setup_missing(settings) -> list[str]` lives in a new
  `web/setup_status.py`: `"LLM key"` when the configured provider's key is
  empty (`openrouter_api_key` for `openrouter`, `anthropic_api_key` for
  `anthropic`); `"Alpaca keys"` when either `alpaca_api_key` or
  `alpaca_secret_key` is empty.
- Exempt paths: `/setup*`, `/login`, `/logout`, `/static/*`, `/a/*`
  (approve-by-link must keep working for an unfinished setup), `/healthz`,
  and every `POST` (only GET navigations redirect, so a form
  post never silently vanishes into a 302).
- The gate reads `app_state` through the existing holder; it must not open
  its own connection.

### Page and steps

One router `web/routes/setup.py`, one template `setup.html`, steps via
`?step=1..4` (default: the first incomplete step). A progress header shows
the four step names with the current one highlighted. All copy English.

**Step 1 — LLM.** Radio: OpenRouter (default) / Anthropic. Under each, a
three-line "where to get a key" card with the signup URL
(`https://openrouter.ai/keys`, `https://console.anthropic.com/settings/keys`).
One password input for the key (masked current value shown like Settings).
One sentence on the three model tiers (chat / review / memory) with the
current defaults; no model pickers here (Settings has them).
Buttons: **Test** (htmx `POST /setup/test-llm`, tests the typed value,
persists nothing, renders `_setup_test_result.html`: "OK · model X
replied" or the error), **Save & continue** (persists via `SettingsStore`,
rebuilds components, → step 2), **Skip for now**.

**Step 2 — Paper account (Alpaca).** Four numbered cards:
1. Create a free account at `https://alpaca.markets`.
2. In the dashboard, switch the top-right toggle to **Paper Trading**.
3. Open *API Keys* → **Generate New Keys**; copy both values now (the
   secret is shown once).
4. Paste them below.
Inputs: API key, Secret key (password, masked current values).
Buttons: **Test connection** (htmx `POST /setup/test-broker`: builds a
throwaway Alpaca broker from the typed values, calls `get_account()`,
renders "Connected · equity $100,000.00" or the error), **Save &
continue**, **Skip for now**.

**Step 3 — Shadow account.** Two sentences: *Shadow mirrors your real
brokerage. Every buy or sell the agent or a rule decides on is recorded
here and you are told to place it yourself — nothing is ever routed.*
Then the shadow ledger summary (cash, position count — same helper as
Settings) and three actions:
- **Open Chat** → `/chat?hint=import` with the account cookie set to
  `shadow` (the route sets the cookie then redirects, reusing
  `/account/switch`).
- **Upload CSV** → the existing `/settings/shadow/csv-preview` fragment
  embedded inline (same form, same target div, same approval flow).
- **Skip for now**.
Continue → step 4.

**Step 4 — Done.** Checklist with links: pair Telegram (Settings →
Telegram), add a notification channel (Settings → Notifications), write
your first strategy (Chat). **Finish** → sets `setup_dismissed=1` →
`/`.

### Persistence and re-entry

- `POST /setup/step/{n}` writes only existing `Settings` fields through
  `SettingsStore` (the quote-safe `.env` writer Settings uses) and calls
  `holder.rebuild()`. No new settings fields. No trading parameter is
  touched.
- `POST /setup/skip` and `POST /setup/finish` write
  `app_state["setup_dismissed"] = "1"`.
- Settings → Access gains a permanent link **Re-run setup** → `/setup?step=1`
  (works regardless of the flag; the flag only controls redirects).
- `/setup` itself never redirects, even when nothing is missing, so the
  re-run link always lands.

### Banner

`nav_context` computes `setup_missing` (same helper). When non-empty and
the current page is not `/setup`, `base.html` renders one muted banner
under the nav: *Setup incomplete — LLM key, Alpaca keys missing ·
[Finish setup](/setup)*. Hidden on the wizard and login pages.

### Security

- `/setup*` sits behind the same token auth as every page; same-origin
  check on every POST (existing middleware).
- Test endpoints use typed values only, persist nothing, and never echo
  the key back; error messages are passed through `describe_validation_error`
  / the existing broker error sanitizer.
- Masks use the existing `masks[...]` helper; inputs never carry `value=`.

---

## ② Per-account hints

- **Chat empty state** (`chat.html`): when the current account's
  conversation has no turns, render an `onboarding` card above the input
  (not part of the conversation, not mirrored). Shadow: *Tell me what you
  hold — paste your positions, type them, or attach a screenshot.* with
  three clickable examples that fill the input (`I own 10 NVDA at 118.40
  and 5,000 cash.`, `Here is my portfolio: …`, `Set my cash to 25,000`).
  Paper: *Ask me to look at a ticker, draft a strategy, or explain what I
  can do.* with three examples. `?hint=import` keeps the shadow card open
  and focuses the attach button even when turns exist.
- **Dashboard**: when Alpaca keys are missing on paper, the broker-failure
  slot renders a guidance card *Connect your Alpaca paper account →
  Setup step 2*. The existing shadow empty-ledger card gains "…or attach
  a screenshot in Chat".
- **Settings → Brokerage**: a `help-toggle` `<details>` above the Alpaca
  inputs with the same four signup steps as wizard step 2 (one shared
  include `_alpaca_signup_steps.html`).

---

## ③ Image attachments in chat

### Web UI

- `chat.html` form becomes `enctype="multipart/form-data"` (htmx
  `hx-encoding="multipart/form-data"`); a 📎 button opens a hidden
  `<input type="file" accept="image/png,image/jpeg,image/webp" multiple>`;
  paste and drag-drop onto the input add files the same way. A thumbnail
  strip shows pending files with a remove ×. Client-side checks mirror the
  server limits and show the reason inline.
- `POST /chat/send` accepts `message: str` + `images: list[UploadFile]`.
  Server validation (authoritative): count ≤ 4, each ≤ 5 MB (read with a
  hard cap, reject on overflow), MIME determined by magic bytes (PNG
  `89 50 4E 47`, JPEG `FF D8 FF`, WebP `RIFF….WEBP`), not by the declared
  type or filename. A message may be images only (empty text → default
  text "Here is an image.").

### Data flow

```
ChatService.send(text, source, images=[ImageAttachment(data, mime, name)])
  → AgentSession.run_turn(text, extra, images=images)
```

- `ImageAttachment` is a frozen dataclass in `agent/attachments.py`
  (`data: bytes, mime: str, name: str`; `size` property; `placeholder()`
  → `[image: positions.png, 312 KB]`).
- `run_turn` appends the user message with `content=text`,
  `display=f"{placeholders} {text}"`, and a transient key `images`.
  `_protocol_only` becomes `_protocol_message(m)`: when `images` is
  present it emits `content` as a list of parts —
  `[{"type": "image", "mime": ..., "data": bytes}, …, {"type": "text", "text": text}]`
  in the unified format; each LLM client converts parts to its own shape
  (Anthropic: `image` block with base64 `source`; OpenAI-compat:
  `image_url` with a `data:` URL). Text-only messages are unchanged.
- **`run_turn` pops `images` from the message in a `finally` before
  returning** (success or error), so the in-memory history, the compactor
  summaries, the `conversations` table, the FTS index, and the Telegram
  mirror only ever see `content`/`display`. A test asserts no `images` key
  and no image bytes survive anywhere after the turn.
- Later turns do not resend the image; the model already turned it into
  tool calls and prose, and corrections are text.

### Model cannot read images

LLM clients raise `LLMError` as today. `ChatService.send` maps an error
whose message matches the providers' "image"/"vision"/"modality"
patterns (or a 400 on a request that carried images) to the reply
*This model can't read images — switch CHAT_MODEL to a vision-capable
model in Settings, or type the positions instead.* The turn is recorded
with that reply so the user sees it in history. Other errors keep the
existing handling. The 📎 button shows a hover hint when the OpenRouter
catalog entry for the current chat model lacks an image input modality
(informational only; never blocks).

### Telegram

- The poller handles updates with `message.photo` (largest size) or
  `message.document` whose `mime_type` is one of the three image types;
  `caption` is the text. Up to 4 media in one album are joined by
  `media_group_id` within the same poll batch; beyond that, reply
  *Up to 4 images per message.*
- Download via `getFile` + the file URL into memory with the 5 MB cap; on
  overflow reply *Image too large (max 5 MB).* Bytes are never written to
  disk or logged (the existing token scrubber already covers the file URL,
  which embeds the bot token).
- Same `ChatService.send(..., source="telegram", images=...)`. The mirror
  placeholder text is all the web side ever sees.

### Agent guidance

`IDENTITY`/system prompt gains a short section: *When the user attaches a
brokerage screenshot, read every row (ticker, quantity, average cost) and
the cash balance, first restate the table you read so the user can
correct it, then call `shadow_set_position` for each row and
`shadow_set_cash` once. Never guess a value you cannot read — ask.* Paper
chats have no ledger tools, so a screenshot there can only be discussed.

### Invariants (unchanged)

- Images add no write capability: every ledger change still goes through
  the existing tools → `shadow_edit` approval → applier.
- Web never calls `Executor.execute()`.
- No credential or image bytes in logs, DB, FTS, or mirrors.
- Zero new dependencies.

---

## ④ Error handling summary

| Case | Behaviour |
|---|---|
| Wizard test fails | Inline result with sanitized error; nothing saved |
| Wizard save fails (`.env` write) | Same error surface as Settings save; stay on step |
| Image too many / too big / not an image | 400 with inline message (web) or bot reply (Telegram); no turn recorded |
| Model rejects images | Turn recorded with the "can't read images" reply |
| LLM error mid-turn with images | Existing error path; `images` still popped (`finally`) |
| Telegram `getFile` failure | Bot reply *Couldn't download that image — try again.* |

## ⑤ Testing

- **Gate matrix**: {LLM missing, Alpaca missing, both, none} × {dismissed,
  not} × {GET page, POST, exempt path} → redirect or not.
- **Steps**: each save writes only the expected `.env` keys via
  `SettingsStore`; masks shown, no `value=`; Skip/Finish set the flag;
  `/setup` never redirects; re-run link present in Settings.
- **Test endpoints**: fake LLM / fake broker → OK copy; error → sanitized
  copy; nothing persisted.
- **Banner**: content per missing set; absent on `/setup` and when
  nothing missing; `assert_english_only` on wizard, banner, hints.
- **Chat hints**: shadow vs paper copy; hidden once turns exist; `?hint=import`.
- **Images**: server rejects count/size/magic-byte failures; accepted
  images reach the LLM as parts (ScriptedLLM asserts the parts); both
  clients' conversion; `images` absent from history/DB/FTS/mirror after
  the turn (and after an error); "can't read images" mapping; Telegram
  photo/document/album/oversize paths; placeholder in mirror.
- **Invariants**: no new `Executor` caller; no ledger write outside the
  applier (existing audit test extended).

## ⑥ Implementation order

Gate + status helper → wizard steps 1–4 + test endpoints → banner +
Settings link → per-account hints → attachments (dataclass, run_turn,
client conversion) → web upload → Telegram photos → agent guidance → docs
(README Two Accounts + Getting started, CHANGELOG).
