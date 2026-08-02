from __future__ import annotations

import json
import sys
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

# SUMMARY_PROMPT's 400-word cap is ~600 tokens at estimate_tokens' rate. A
# first-ever compaction has no previous frame to size against (the diff
# below would be 0), so reserve that fixed amount instead of under-reserving
# for the summary about to be written.
FIRST_SUMMARY_RESERVE_TOKENS = 600

# `target` must stay above zero even when frame_cost eats the whole budget,
# or `_cut_index` can never find a fitting suffix and compaction deadlocks
# permanently. Not reachable at the 60k default; guards small configured
# budgets. A floor, not a guarantee a cut is found.
MIN_CUT_TARGET_TOKENS = 500


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
        self._store_failure_warned = False
        self._alignment_warned = False

    def _warn_store_failure(self, exc: Exception) -> None:
        # One-shot per instance, mirroring AgentSession._append: a wedged
        # session should say so once, not spam stderr every turn.
        if not self._store_failure_warned:
            self._store_failure_warned = True
            print(f"[warning] compaction skipped: conversation store error: {exc}",
                  file=sys.stderr)

    def _warn_alignment_mismatch(self) -> None:
        if not self._alignment_warned:
            self._alignment_warned = True
            print("[warning] compaction skipped: in-memory history has drifted "
                  "from the stored conversation", file=sys.stderr)

    def maybe_compact(self, conversation_id: int,
                      history: list[dict]) -> tuple[list[dict], list[dict]]:
        """Returns `(context_to_send, history_still_in_memory)`.

        The caller MUST adopt the second value (assign it back over its own
        history) before calling again. `history` is only a valid index space
        for `cut` while it is exactly the transcript from the current marker
        (`since`) forward — every branch below that does *not* advance the
        marker returns `history` unchanged, and the one branch that does
        advance it returns the trimmed tail instead. That makes "the
        caller's copy stays aligned with the store" true by construction
        rather than something each call site has to remember to uphold. Every
        store call below is guarded on that same basis: a persistence failure
        must degrade like an LLM failure (oversized-but-correct context,
        marker untouched), never propagate and end the conversation.
        """
        try:
            previous, since = self.store.summary(conversation_id)
        except Exception as exc:  # noqa: BLE001 — must never end the chat, see class docstring
            self._warn_store_failure(exc)
            return list(history), history
        framed = self._frame(previous, history)
        if estimate_tokens(framed) <= self.budget_tokens:
            return framed, history

        # The overflow check above counts the leading summary frame's tokens
        # toward the budget (it's part of `framed`), but a plain cut over
        # `history` doesn't know that frame exists — so reserve that same
        # cost here (the current frame is a proxy for the new one, since both
        # are bounded by the same summarization prompt). With no previous
        # frame to measure, fall back to a fixed reserve for the summary
        # about to be written instead.
        frame_cost = max(
            estimate_tokens(framed) - estimate_tokens(history) if previous.strip() else 0,
            FIRST_SUMMARY_RESERVE_TOKENS)
        target = max((self.budget_tokens * 2) // 3 - frame_cost, MIN_CUT_TARGET_TOKENS)
        cut = _cut_index(history, target)
        if cut == 0:
            return framed, history  # nothing can be dropped without splitting a tool call

        # `turn_ids` (fetched fresh from `since`) must be the same length as
        # `history`, or `cut` no longer indexes what the store thinks is
        # unsummarized (a caller that didn't adopt a trimmed `history`, or the
        # store legitimately falling behind under a persistence failure — see
        # AgentSession._append). We can't tell those apart, so treat either
        # as the same case as an LLM failure. Checked before the flush hook
        # or the summarizing LLM call so a misalignment doesn't waste both.
        try:
            turn_ids = [tid for tid, _ in
                        self.store.history_with_ids(conversation_id, after_turn_id=since)]
        except Exception as exc:  # noqa: BLE001 — see _warn_store_failure
            self._warn_store_failure(exc)
            return framed, history
        if len(turn_ids) != len(history):
            self._warn_alignment_mismatch()
            return framed, history

        older, newer = history[:cut], history[cut:]
        if self.on_before_compact is not None:
            # Let the agent write durable conclusions to memory before the raw
            # messages leave the context. Compacting first would silently lose
            # preferences the user stated once and never repeated.
            self.on_before_compact(older)

        summary = self._summarize(previous, older)
        if summary is None:
            return framed, history  # LLM failure: oversized-but-correct context

        through = turn_ids[cut - 1]
        try:
            self.store.set_summary(conversation_id, summary, through)
        except Exception as exc:  # noqa: BLE001 — see _warn_store_failure
            self._warn_store_failure(exc)
            return framed, history
        return self._frame(summary, newer), newer

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
