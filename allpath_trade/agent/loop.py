from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from allpath_trade.agent.attachments import ImageAttachment, placeholders
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
    """Project a history entry down to the protocol fields, expanding the
    transient `images` key (setup-wizard T5) into the unified list-content
    shape every LLMClient knows how to convert:
    `[{"type": "image", "mime": ..., "data": bytes}, ..., {"type": "text", ...}]`.

    `images` only ever exists on the ONE user message of the turn in
    flight (`run_turn` pops it in a `finally`), so a text-only message --
    every message of every other turn -- takes the untouched str-content
    path and produces a byte-identical request to before this existed."""
    out = {k: message[k] for k in _PROTOCOL_KEYS if k in message}
    images = message.get("images")
    if images:
        out["content"] = [
            *({"type": "image", "mime": i.mime, "data": i.data} for i in images),
            {"type": "text", "text": out.get("content") or ""},
        ]
    return out


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

        `images` are TRANSIENT (spec ③). They are attached to the user
        message dict only AFTER `_append` has already persisted it, so the
        `conversations` row, the FTS index, the compactor's summaries and
        the Telegram mirror never see bytes -- only the `display` string
        (`placeholders(images) + " " + text`) and the plain-text `content`.
        The `finally` below pops the key on every exit path (a normal
        return, the tool-call limit, an LLMError notice, or an exception
        propagating out), so a later turn can never resend the image
        either: the model has already turned it into tool calls and prose,
        and corrections are text."""
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
            message["display"] = f"{placeholders(images)} {user_text}"
        self._append(message)
        if images:
            message["images"] = list(images)
        try:
            return self._run_loop()
        finally:
            message.pop("images", None)

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
            messages = [_protocol_message(m) for m in messages]
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
