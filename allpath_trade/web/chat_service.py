from __future__ import annotations

import threading
from datetime import UTC, datetime

from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.compact import Compactor
from allpath_trade.agent.context import build_system_prompt, load_identity
from allpath_trade.agent.loop import AgentSession
from allpath_trade.agent.memory_tools import register_memory_tools
from allpath_trade.agent.readonly_tools import register_readonly_tools
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.memory.search import SessionSearch
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.web.order_sink import QueueingOrderSink

SNAPSHOT_TTL_SECONDS = 30 * 60


class ChatService:
    """One conversation, forever.

    The user never picks or creates a session — the process resumes the latest
    conversation and the Compactor keeps it inside the context budget. The
    system-prompt snapshot is frozen for cache stability, so it is rebuilt once
    it is older than SNAPSHOT_TTL_SECONDS; a long-lived server would otherwise
    reason about yesterday's positions.

    This object is shared by every request in the process. `_turn_lock`
    serializes anything that mutates the underlying AgentSession's history
    (a chat turn, or an out-of-band note like an approval) so two concurrent
    requests can never interleave writes to the same in-memory list — the
    second waits for the first to finish rather than racing it."""

    def __init__(self, holder) -> None:
        self.holder = holder
        self._lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._session: AgentSession | None = None
        self._built_at = 0.0
        self.activity: list[str] = []

    def _stale(self) -> bool:
        age = datetime.now(UTC).timestamp() - self._built_at
        return age > SNAPSHOT_TTL_SECONDS

    def session(self) -> AgentSession:
        with self._lock:
            if self._session is None or self._stale():
                self._session = self._build()
                self._built_at = datetime.now(UTC).timestamp()
            return self._session

    def _build(self) -> AgentSession:
        from allpath_trade.llm.factory import build_llm

        c = self.holder.get()
        store = ConversationStore(c.conn)
        conversation_id = store.latest() or store.start()

        registry = ToolRegistry()
        register_readonly_tools(registry, data=c.data, broker=c.broker,
                                journal=c.journal, strategies=c.strategies,
                                queue=c.queue)
        register_memory_tools(registry, memory=c.memory, search=SessionSearch(c.conn))
        register_action_tools(
            registry, strategies=c.strategies, executor=c.executor,
            confirm=lambda _prompt: False,
            order_sink=QueueingOrderSink(c.queue, c.gate, c.broker, c.data,
                                         c.journal, conversation_id))

        prompt = build_system_prompt(
            identity=load_identity(), broker=c.broker, journal=c.journal,
            strategies=c.strategies, queue=c.queue, memory=c.memory)
        compactor = Compactor(build_llm(c.settings, tier="memory"), store,
                              budget_tokens=c.settings.context_budget_tokens)
        return AgentSession(build_llm(c.settings, tier="chat"), registry, prompt,
                            store=store, conversation_id=conversation_id,
                            compactor=compactor,
                            on_tool=lambda call: self.activity.append(call.name))

    def send(self, text: str) -> str:
        with self._turn_lock:
            self.activity = []
            return self.session().run_turn(text)

    def messages(self) -> list[dict]:
        return list(self.session().history)

    def note_resolution(self, line: str) -> None:
        """Record an out-of-band event (an approval, a fill) in the transcript
        so the agent sees it on its next turn. Shares `_turn_lock` with
        `send()`: an approval clicked mid-turn must not append into the
        history while `run_turn` is iterating over it."""
        with self._turn_lock:
            session = self.session()
            session._append({"role": "user", "content": f"[system] {line}"})
