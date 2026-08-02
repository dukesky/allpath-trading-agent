from __future__ import annotations

import sys
from collections.abc import Callable

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import LLMClient, LLMError, ToolCall
from allpath_trade.store.conversations import ConversationStore

LIMIT_NOTICE = "(stopped: tool-call limit reached — ask me to continue if needed)"


class AgentSession:
    """One conversation with the agent. System prompt is frozen at
    construction (stable prefix); history is unified-format messages."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, system_prompt: str,
                 store: ConversationStore | None = None,
                 conversation_id: int | None = None, max_iters: int = 15,
                 on_tool: Callable[[ToolCall], None] | None = None,
                 compactor: object | None = None) -> None:
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

    def run_turn(self, user_text: str) -> str:
        self._append({"role": "user", "content": user_text})
        for _ in range(self.max_iters):
            context = self.history
            if self.compactor is not None and self.conversation_id is not None:
                context = self.compactor.maybe_compact(
                    self.conversation_id, self.history)
            messages = [{"role": "system", "content": self.system_prompt}, *context]
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
