from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import LLMClient, LLMError, ToolCall
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


def _protocol_only(message: dict) -> dict:
    return {k: message[k] for k in _PROTOCOL_KEYS if k in message}


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

    def run_turn(self, user_text: str, extra: dict | None = None) -> str:
        """`extra` merges presentation-only bookkeeping keys (e.g.
        ChatService's `source`) onto the appended user message dict --
        `_protocol_only` below strips them before any LLM call, same as
        `note_resolution`'s `kind`/`display`. `run_turn` (not the caller)
        owns the append, so this is the only seam that can attach them."""
        extra = extra or {}
        # Reviewer-requested (Telegram plan, Task 4 review): an `extra` key
        # that happened to be named `role`/`content`/`tool_call_id`/
        # `tool_calls` would silently clobber the real protocol field via
        # dict-unpacking order in the `_append` call below -- a caller bug
        # that would only ever surface as a mangled message sent to the LLM,
        # far from where the bad `extra` dict was built. Fail loudly here,
        # at the one seam that ever merges `extra` in, instead.
        collision = set(extra) & set(_PROTOCOL_KEYS)
        assert not collision, f"extra keys collide with protocol keys: {collision}"
        self._append({"role": "user", "content": user_text, **extra})
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
            messages = [_protocol_only(m) for m in messages]
            try:
                resp = self.llm.complete(messages, tools=self.registry.specs())
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
