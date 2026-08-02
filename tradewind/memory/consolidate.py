from __future__ import annotations

import sqlite3

from tradewind.agent.loop import AgentSession
from tradewind.agent.memory_tools import register_memory_tools
from tradewind.agent.tools import ToolRegistry
from tradewind.llm.base import LLMClient
from tradewind.memory.observations import ObservationLog
from tradewind.memory.store import MemoryStore
from tradewind.store.journal import TradeJournal

CONSOLIDATE_PROMPT = """\
You are the memory consolidator for a trading agent. Below are recent raw
events and the current curated memory. Distill DURABLE facts into curated
memory using the memory_update tool (layers: profile, strategy, stock,
lesson). Rules: write your OWN concise conclusions — never copy external or
quoted content; prefer replace over add when refining an existing entry;
skip noise. When finished reply with one short text summary line.

## Recent events
{events}

## Current memory (profile)
{profile}
"""

POST_CHAT_PROMPT = """\
You are the memory consolidator. From this conversation transcript, extract
ONLY preferences or decisions the user explicitly stated (risk tolerance,
goals, habits, standing decisions) and record them with memory_update
(usually layer=profile). If none, make no updates. Finish with one short
summary line.

## Transcript
{transcript}
"""

MARKER = "consolidation run"


class Consolidator:
    def __init__(self, llm: LLMClient, memory: MemoryStore,
                 observations: ObservationLog, journal: TradeJournal,
                 conn: sqlite3.Connection, max_updates: int = 20) -> None:
        self.llm = llm
        self.memory = memory
        self.observations = observations
        self.journal = journal
        self._conn = conn
        self.max_updates = max_updates

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        register_memory_tools(registry, memory=self.memory)
        return registry

    def _last_marker_ts(self) -> str | None:
        row = self._conn.execute(
            "SELECT ts FROM observations WHERE source='consolidator'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        return row["ts"] if row else None

    def run_daily(self) -> str:
        try:
            events: list[str] = []
            for r in self.observations.recent(since_iso=self._last_marker_ts()):
                events.append(f"[{r['source']}/{r['subject'] or '-'}] {r['text']}")
            for r in self.journal.recent(limit=20):
                events.append(f"[trade] {r['ts'][:19]} {r['side']} {r['ticker']}"
                              f" [{r['status']}] {r['reason']}")
            # No short-circuit on empty events: the daily run always talks to
            # the LLM (even to say "nothing happened"), so infra/LLM failures
            # surface every day rather than only on days with new events.
            prompt = CONSOLIDATE_PROMPT.format(
                events="\n".join(events[-100:]),
                profile=self.memory.render_for_context("profile") or "(empty)")
            session = AgentSession(self.llm, self._registry(), prompt,
                                   max_iters=self.max_updates)
            summary = session.run_turn("Consolidate now.")
            self.observations.add("consolidator", f"{MARKER}: {summary[:200]}")
            return summary
        except Exception as exc:  # noqa: BLE001 — consolidation must degrade silently
            return f"consolidation failed: {exc}"

    def run_post_chat(self, transcript: list[dict]) -> str:
        try:
            lines = [f"{m['role']}: {m['content']}" for m in transcript
                     if m.get("role") in ("user", "assistant")
                     and isinstance(m.get("content"), str) and m["content"].strip()]
            if not lines:
                return "nothing to consolidate"
            prompt = POST_CHAT_PROMPT.format(transcript="\n".join(lines[-60:]))
            session = AgentSession(self.llm, self._registry(), prompt, max_iters=6)
            return session.run_turn("Extract and record now.")
        except Exception as exc:  # noqa: BLE001
            return f"consolidation failed: {exc}"
