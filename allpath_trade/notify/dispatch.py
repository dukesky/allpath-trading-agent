"""Single choke point for a "review queued"/"receipt" notification reaching
BOTH channels this app can push to: email/ntfy (`notify.base.Notifier`,
unchanged from before this module existed) and, when a chat is paired, the
Telegram bot (new).

Three call sites queue a review today -- `sentinel.py` (a soft-rule/confirm
trigger), `web/order_sink.py` (a chat order proposal), and
`agent/action_tools.py` (a chat/reflection strategy draft) -- and each used
to own its own little "build subject/body, maybe call notifier.send" stanza.
`notify_review_queued` below is the ONE place that stanza now lives; each
call site still builds its own `events.review_queued(...)` (or
`events.order_result(...)`) subject/body -- the kind-specific text only that
call site knows how to construct -- and hands it to this module.

Every function here takes plain primitives (`queue`, `notifier`, `app_state`,
`telegram_bot_token`), not a `Components` bag: `sentinel.py` and
`agent/action_tools.py` are non-web modules that must not import the `web`
package's `Components`/`ComponentHolder` machinery, and threading four
explicit values through is cheap.

Telegram delivery is entirely best-effort here: a missing bot token, no
paired chat, a `ReviewQueue.get` failure, or any exception from the
`TelegramAPI` calls themselves (already best-effort/never-raising on their
own, see telegram.py) all degrade silently to "no Telegram push this time" --
this module never lets a Telegram hiccup take down the email/ntfy leg or the
caller's own state transition, which by the time any of these functions run
has already committed."""

from __future__ import annotations

from allpath_trade.notify.base import Notifier
from allpath_trade.notify.events import _prefix
from allpath_trade.store.accounts import ACCOUNTS
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.reviews import ReviewError, ReviewQueue
from allpath_trade.telegram import TelegramAPI
from allpath_trade.web.markdown import to_telegram_html


def _prefixed_for_telegram(account: str, body: str) -> str:
    """shadow-dual-active T7 (spec §⑤: "Telegram 按钮消息同样前缀"): Telegram
    never renders the email/ntfy `subject` at all -- `push_telegram_review_
    queued`/`push_telegram_receipt` below only ever send `body` -- so the
    account has to be visible IN the body for a Telegram-only reader to
    know which account a push concerns, the same way telegram.py's own
    `_handle_chat_text`/`_resolve_review_callback` already prefix every
    message THEY construct directly with `[Paper]`/`[Shadow]`.

    Reuses `events._prefix` (the exact same helper every subject prefix in
    this codebase now comes from) rather than re-deriving the bracket/
    capitalization shape here, and treats an ALREADY-PREFIXED body as
    final: a body opening with ANY known account's prefix is returned
    untouched, not just one opening with the prefix this call was about to
    add. The narrower same-prefix-only guard this replaces turned a
    `("shadow", "[Paper] ...")` call into `"[Shadow] [Paper] ..."` -- two
    contradictory account labels on one message, with the wrong one
    leading. Whoever labelled the body knew which account it concerned;
    this function's `account` argument is the weaker guess of the two."""
    prefix = _prefix(account)
    if not prefix:
        return body
    if any(body.startswith(_prefix(known)) for known in ACCOUNTS):
        return body
    return prefix + body


def _telegram_target(app_state: AppState | None,
                     telegram_bot_token: str) -> tuple[TelegramAPI, str] | None:
    """The (api, chat_id) pair to push to, or `None` when Telegram is off
    (no bot token) or nothing is paired yet -- the one gate every push
    function below shares."""
    if not telegram_bot_token or app_state is None:
        return None
    chat_id = app_state.get(TELEGRAM_CHAT_ID_KEY)
    if not chat_id:
        return None
    return TelegramAPI(telegram_bot_token), chat_id


def push_telegram_review_queued(*, queue: ReviewQueue, app_state: AppState | None,
                                telegram_bot_token: str, review_id: int,
                                body: str, account: str) -> None:
    """Push a queued-review summary to the paired Telegram chat with inline
    Approve/Reject buttons. `body` is the same kind-specific summary text
    `events.review_queued(...)` already built for the email leg -- rendered
    through `to_telegram_html` (never sent as raw Markdown/plain text) so it
    reads the same way a normal agent reply does.

    `callback_data` is `rv:<approve|reject>:<review_id>:<nonce>`, where
    `nonce` is the first 16 hex characters of the row's OWN
    `approval_token_hash` (already minted and persisted by
    `ReviewQueue.add`/`add_strategy_revision` regardless of whether an
    approve-by-link URL was ever built from it -- see `_issue_token`). The
    real authorization boundary is chat-id + from-id binding (only the
    paired chat can ever trigger a callback the poller acts on at all, see
    `TelegramPoller._handle_callback_query`) -- the nonce is a belt on top
    of that against a stale/replayed callback resolving to the wrong row,
    not the security boundary itself.

    Best-effort end to end: never raises, silently does nothing when
    Telegram is off/unpaired or the review row can't be read back."""
    try:
        target = _telegram_target(app_state, telegram_bot_token)
        if target is None:
            return
        api, chat_id = target
        try:
            row = queue.get(review_id)
        except ReviewError:
            return
        nonce = (row["approval_token_hash"] or "")[:16]
        if not nonce:
            return
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"rv:approve:{review_id}:{nonce}"},
            {"text": "❌ Reject", "callback_data": f"rv:reject:{review_id}:{nonce}"},
        ]]}
        api.send_message(chat_id, to_telegram_html(_prefixed_for_telegram(account, body)),
                         reply_markup=keyboard)
    except Exception:  # noqa: BLE001, S110 — best-effort, never break the caller
        pass


def push_telegram_receipt(*, app_state: AppState | None, telegram_bot_token: str,
                          body: str, account: str) -> None:
    """Push a plain receipt (no buttons) to the paired Telegram chat --
    used for an order_result event so an auto-executed hard rule ("自动执行了
    也要通知我") reaches Telegram too, not just email/ntfy. Same best-effort
    contract as `push_telegram_review_queued`."""
    try:
        target = _telegram_target(app_state, telegram_bot_token)
        if target is None:
            return
        api, chat_id = target
        api.send_message(chat_id, to_telegram_html(_prefixed_for_telegram(account, body)))
    except Exception:  # noqa: BLE001, S110 — best-effort, never break the caller
        pass


def notify_review_queued(*, queue: ReviewQueue, notifier: Notifier | None,
                         app_state: AppState | None, telegram_bot_token: str,
                         review_id: int, subject: str, body: str, account: str,
                         notify_email: bool = True) -> None:
    """Notify BOTH channels that a review is waiting for approval.

    `notify_email` is the CALLER's own gating decision -- each of the three
    call sites keeps its own existing policy (sentinel.py gates on
    `doc.notify_email`; action_tools.py's chat drafts and order_sink.py's
    chat order proposals always attempt it, matching how neither of those
    two web-chat-initiated paths could be silenced by a `notify_email`
    field before this module existed either) -- this function only executes
    it, never overrides it. `notifier is None` (no channel configured) is
    always a silent no-op for the email leg, same as before.

    The Telegram leg is unconditional whenever a chat is paired: it is not
    gated by `notify_email` at all, by design -- the whole point of pairing
    is a channel the queueing event itself can never silence (mirrors
    action_tools.py's existing "must never be silenceable by the proposal
    itself" reasoning for its own email leg)."""
    if notify_email and notifier is not None:
        notifier.send(subject, body)
    push_telegram_review_queued(queue=queue, app_state=app_state,
                                telegram_bot_token=telegram_bot_token,
                                review_id=review_id, body=body, account=account)
