from __future__ import annotations

from tradewind.agent.tools import ToolRegistry
from tradewind.llm.base import LLMClient, LLMError
from tradewind.store.conversations import ConversationStore

LIMIT_NOTICE = "(stopped: tool-call limit reached — ask me to continue if needed)"


class AgentSession:
    """One conversation with the agent. System prompt is frozen at
    construction (stable prefix); history is unified-format messages."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry, system_prompt: str,
                 store: ConversationStore | None = None,
                 conversation_id: int | None = None, max_iters: int = 15) -> None:
        self.llm = llm
        self.registry = registry
        self.system_prompt = system_prompt
        self.store = store
        self.conversation_id = conversation_id
        self.max_iters = max_iters
        self.history: list[dict] = []
        if store is not None and conversation_id is not None:
            self.history = store.history(conversation_id)

    def _append(self, message: dict) -> None:
        self.history.append(message)
        if self.store is not None and self.conversation_id is not None:
            self.store.append(self.conversation_id, message)

    def run_turn(self, user_text: str) -> str:
        self._append({"role": "user", "content": user_text})
        for _ in range(self.max_iters):
            messages = [{"role": "system", "content": self.system_prompt},
                        *self.history]
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
                    result = self.registry.execute(call)
                    self._append({"role": "tool", "tool_call_id": call.id,
                                  "content": result})
                continue
            text = resp.text or ""
            self._append({"role": "assistant", "content": text})
            return text
        self._append({"role": "assistant", "content": LIMIT_NOTICE})
        return LIMIT_NOTICE
