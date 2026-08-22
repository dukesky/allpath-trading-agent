from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from allpath_trade.agent.attachments import ImageAttachment, display_for
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import LLMClient, LLMError, LLMImageUnsupported, ToolCall
from allpath_trade.store.conversations import ConversationStore

if TYPE_CHECKING:
    from allpath_trade.agent.compact import Compactor

LIMIT_NOTICE = "(stopped: tool-call limit reached — ask me to continue if needed)"

# The only fields any LLMClient's chat-completions message schema accepts.
# History entries can carry extra bookkeeping fields that must never reach a
# provider -- e.g. ChatService.note_resolution's `kind`/`display`, where
# `display` is deliberately the *unfenced* text (see chat_service.py) kept
# only for template rendering. Forwarding it verbatim would ship the exact
# string fence_external exists to neutralize, and a strict endpoint may
# reject an unknown field outright. Projecting once here, at the one place
# `messages` is assembled for `llm.complete`, keeps every LLMClient
# implementation protocol-clean without each one having to know which
# fields on a history dict are presentation-only.
_PROTOCOL_KEYS = ("role", "content", "tool_call_id", "tool_calls")


def _protocol_message(message: dict) -> dict:
    """Project a history entry down to the protocol fields. Pure: it never
    looks at, or introduces, anything that isn't already on the history
    dict -- image bytes are injected downstream, by `_with_images`."""
    return {k: message[k] for k in _PROTOCOL_KEYS if k in message}


def _with_images(messages: list[dict],
                 images: list[ImageAttachment] | None) -> list[dict]:
    """Attach the turn's image parts to the LAST user entry of an outgoing
    request, in the unified shape every LLMClient converts:
    `[{"type": "image", "mime": ..., "data": bytes}, ..., {"type": "text", ...}]`.

    Called with the turn's images exactly ONCE -- on the first
    `llm.complete` of the turn, after which `_run_loop` drops them (see
    there for why). Every later iteration passes `images=None` and gets the
    list back untouched.

    Injecting here -- into the throwaway list built for one `llm.complete`
    call -- rather than onto the history dict is what keeps the bytes out
    of everything that reads history. Review finding (Important 1): with
    `images` on the message, `Compactor.estimate_tokens` (which does
    `json.dumps(m, default=str)` over every message) valued a 3 MB
    screenshot at ~3.9M tokens, so `_cut_index` could never find a fitting
    suffix -- compaction AND the `on_before_compact` memory flush silently
    stopped happening for that turn, after burning seconds re-serializing
    the bytes on every iteration, under ChatService's `_turn_lock`. It also
    means `ChatService.messages()` cannot observe bytes mid-turn.

    Scans backwards for the last `user` entry rather than assuming the last
    element: within a tool loop the request ends with `tool` results, and
    the turn's own user message is the newest `user` one either way (an
    out-of-band `note_resolution` append is blocked behind the same
    `_turn_lock` for the duration of the turn). An images-only message
    (empty text -- spec ③ allows it) emits NO text part: Anthropic rejects
    an empty text block with a 400 that no "unsupported" mapping catches.
    """
    if not images:
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        parts: list[dict] = [{"type": "image", "mime": im.mime, "data": im.data}
                             for im in images]
        text = messages[i].get("content") or ""
        if text:
            parts.append({"type": "text", "text": text})
        messages[i] = {**messages[i], "content": parts}
        break
    return messages


class AgentSession:
    """One conversation with the agent. System prompt is frozen at
    construction (stable prefix); history is unified-format messages."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, system_prompt: str,
                 store: ConversationStore | None = None,
                 conversation_id: int | None = None, max_iters: int = 15,
                 on_tool: Callable[[ToolCall], None] | None = None,
                 compactor: Compactor | None = None) -> None:
        self.llm = llm
        self.registry = registry
        self.system_prompt = system_prompt
        self.store = store
        self.conversation_id = conversation_id
        self.max_iters = max_iters
        self.on_tool = on_tool
        self.compactor = compactor
        self.persistence_failed = False
        # The in-flight turn's attachments, if any -- see run_turn. Never
        # on a history dict: only `_with_images` reads this, when it builds
        # the throwaway `messages` list for the turn's first `llm.complete`
        # call, which is also where it is consumed.
        self._pending_images: list[ImageAttachment] | None = None
        self.history: list[dict] = []
        if store is not None and conversation_id is not None:
            _, through = store.summary(conversation_id)
            self.history = store.history(conversation_id, after_turn_id=through)

    def _append(self, message: dict) -> None:
        """Record a message. History lives in memory first: a persistence
        failure (disk full, moved/locked db) degrades to an in-memory session
        with one warning — it must never end a conversation in progress."""
        self.history.append(message)
        if self.store is None or self.conversation_id is None:
            return
        try:
            self.store.append(self.conversation_id, message)
        except Exception as exc:  # noqa: BLE001 — logging must not kill the chat
            if not self.persistence_failed:
                self.persistence_failed = True
                print(f"[warning] conversation not being saved: {exc}",
                      file=sys.stderr)

    def run_turn(self, user_text: str, extra: dict | None = None,
                 images: list[ImageAttachment] | None = None) -> str:
        """`extra` merges presentation-only bookkeeping keys (e.g.
        ChatService's `source`) onto the appended user message dict --
        `_protocol_message` below strips them before any LLM call, same as
        `note_resolution`'s `kind`/`display`. `run_turn` (not the caller)
        owns the append, so this is the only seam that can attach them.

        `images` are TRANSIENT (spec ③) and are deliberately NOT stored on
        the message dict at all: they are held on the session
        (`self._pending_images`), injected by `_with_images` into the FIRST
        outgoing request of the turn and dropped there, and cleared again by
        the `finally` below on every exit path. So the history dict, the
        `conversations` row, the FTS index, the compactor and the Telegram
        mirror only ever see `content` (plain text) and `display` (`display_for`, the
        placeholder line). A later turn cannot resend the image either: the
        model has already turned it into tool calls and prose, and
        corrections are text."""
        extra = extra or {}
        # Reviewer-requested (Telegram plan, Task 4 review): an `extra` key
        # that happened to be named `role`/`content`/`tool_call_id`/
        # `tool_calls` would silently clobber the real protocol field via
        # dict-unpacking order in the `_append` call below -- a caller bug
        # that would only ever surface as a mangled message sent to the LLM,
        # far from where the bad `extra` dict was built. Fail loudly here,
        # at the one seam that ever merges `extra` in, instead.
        collision = set(extra) & (set(_PROTOCOL_KEYS) | {"images"})
        assert not collision, f"extra keys collide with protocol keys: {collision}"
        message: dict = {"role": "user", "content": user_text, **extra}
        if images:
            # `display` is what the transcript, session search and the
            # mirror show for this turn -- the bytes' only durable trace.
            message["display"] = display_for(images, user_text)
        self._append(message)
        self._pending_images = list(images) if images else None
        try:
            return self._run_loop()
        finally:
            self._pending_images = None

    def _run_loop(self) -> str:
        for _ in range(self.max_iters):
            context = self.history
            if self.compactor is not None and self.conversation_id is not None:
                # `self.history` must stay exactly aligned with the stored
                # summary marker across iterations and across turns — adopt
                # the trimmed history maybe_compact returns rather than
                # keeping the old (possibly now-summarized) messages around.
                # See Compactor.maybe_compact's docstring: without this, the
                # *next* compaction's cut index stops matching what
                # history_with_ids fetches from the marker forward, and
                # turns get silently and permanently dropped.
                context, self.history = self.compactor.maybe_compact(
                    self.conversation_id, self.history)
            messages = [{"role": "system", "content": self.system_prompt}, *context]
            messages = _with_images([_protocol_message(m) for m in messages],
                                    self._pending_images)
            # Whole-branch review (Important 4): the FIRST call of the turn
            # carries the bytes, and only that one. Re-attaching on every
            # iteration re-uploaded the same payload per tool round-trip
            # (4 images x 5 MB x 6 iterations = 120 MB of upload for one
            # screenshot import, paid for again in provider image tokens).
            # The system prompt makes the model restate the table it read
            # before it calls anything (agent/context.py's SCREENSHOT_NOTE),
            # so its own restatement is in the history every later iteration
            # sends -- the picture doesn't have to be. Consumed here rather
            # than in `run_turn`'s `finally`, which still clears it on every
            # exit path (including this one having already done so).
            self._pending_images = None
            try:
                resp = self.llm.complete(messages, tools=self.registry.specs())
            except LLMImageUnsupported:
                # The one LLMError the generic "(llm error: ...)" notice
                # would bury: ChatService owns the user-facing copy for it
                # (attachments.IMAGE_UNSUPPORTED_REPLY) and records the
                # turn itself, so let it through. Only ever raised for a
                # request that actually carried images, i.e. only ever on a
                # ChatService turn -- every other caller (cli, reflect,
                # consolidate) sends text and cannot reach this.
                raise
            except LLMError as exc:
                notice = f"(llm error: {exc})"
                self._append({"role": "assistant", "content": notice})
                return notice
            if resp.tool_calls:
                self._append({
                    "role": "assistant", "content": resp.text,
                    "tool_calls": [c.model_dump() for c in resp.tool_calls]})
                for call in resp.tool_calls:
                    if self.on_tool is not None:
                        self.on_tool(call)
                    result = self.registry.execute(call)
                    self._append({"role": "tool", "tool_call_id": call.id,
                                  "content": result})
                continue
            text = resp.text or ""
            self._append({"role": "assistant", "content": text})
            return text
        self._append({"role": "assistant", "content": LIMIT_NOTICE})
        return LIMIT_NOTICE
