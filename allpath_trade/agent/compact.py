from __future__ import annotations

import json
from collections.abc import Callable

from allpath_trade.llm.base import LLMClient, LLMError
from allpath_trade.store.conversations import ConversationStore

SUMMARY_PROMPT = """\
You are compacting the older part of an ongoing conversation between a user
and their investing copilot so it can be dropped from the context window.

Write a compact briefing in English covering: the user's stated preferences
and constraints, decisions that were made and why, positions and strategies
discussed, and anything the assistant promised to do. Omit small talk and
anything already superseded. Be specific — names, tickers, numbers. No
preamble, no headings, under 400 words.

If an earlier briefing is included below, fold it into the new one rather
than repeating it separately.
"""


def estimate_tokens(messages: list[dict]) -> int:
    """Character count over four. Deliberately crude: a tokenizer dependency
    would buy precision we do not need for a budget threshold."""
    chars = sum(len(json.dumps(m, default=str)) for m in messages)
    return chars // 4


def _cut_index(messages: list[dict], target_tokens: int) -> int:
    """Index of the first message to keep.

    Cuts only immediately before a `user` message: an assistant message that
    carries `tool_calls` must stay with the `tool` results that answer it, and
    a user turn is the one place that boundary is always clean."""
    for i, msg in enumerate(messages):
        if msg.get("role") != "user" or i == 0:
            continue
        if estimate_tokens(messages[i:]) <= target_tokens:
            return i
    return 0


class Compactor:
    """Keeps one endless conversation inside a bounded context.

    The full transcript stays in SQLite and in the FTS5 index — this only
    governs what gets sent to the model."""

    def __init__(self, llm: LLMClient, store: ConversationStore,
                 budget_tokens: int = 60_000,
                 on_before_compact: Callable[[list[dict]], None] | None = None) -> None:
        self.llm = llm
        self.store = store
        self.budget_tokens = budget_tokens
        self.on_before_compact = on_before_compact

    def maybe_compact(self, conversation_id: int, history: list[dict]) -> list[dict]:
        previous, since = self.store.summary(conversation_id)
        framed = self._frame(previous, history)
        if estimate_tokens(framed) <= self.budget_tokens:
            return framed

        target = (self.budget_tokens * 2) // 3
        cut = _cut_index(history, target)
        if cut == 0:
            return framed  # nothing can be dropped without splitting a tool call

        older, newer = history[:cut], history[cut:]
        if self.on_before_compact is not None:
            # Let the agent write durable conclusions to memory before the raw
            # messages leave the context. Compacting first would silently lose
            # preferences the user stated once and never repeated.
            self.on_before_compact(older)

        summary = self._summarize(previous, older)
        if summary is None:
            return framed  # LLM failure degrades to an oversized-but-correct context

        # `history` starts right after the *current* marker (`since`) — the
        # caller (AgentSession) fetches it that way, and callers that don't
        # pass a marker-aligned `history` get a `cut` that's meaningless
        # against the full transcript anyway. Re-fetching ids from `since`
        # rather than from turn 0 keeps this offset consistent with `cut`,
        # which is a local index into `history`, not into the whole table —
        # using the whole table's ids here previously set `through` to the
        # id of the `cut`-th turn *overall*, silently wrong on every
        # compaction after the first.
        turn_ids = [tid for tid, _ in
                    self.store.history_with_ids(conversation_id, after_turn_id=since)][:cut]
        through = turn_ids[-1] if turn_ids else since
        self.store.set_summary(conversation_id, summary, through)
        return self._frame(summary, newer)

    def _frame(self, summary: str, messages: list[dict]) -> list[dict]:
        if not summary.strip():
            return list(messages)
        return [{"role": "system",
                 "content": "Briefing on the earlier part of this conversation:\n"
                            + summary}, *messages]

    def _summarize(self, previous: str, older: list[dict]) -> str | None:
        transcript = "\n".join(
            f"{m.get('role')}: {str(m.get('content') or '')[:2000]}" for m in older)
        prior = f"\n\nEarlier briefing:\n{previous}" if previous.strip() else ""
        try:
            resp = self.llm.complete([
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": f"Conversation:\n{transcript}{prior}"},
            ])
        except LLMError:
            return None
        text = (resp.text or "").strip()
        return text or None
