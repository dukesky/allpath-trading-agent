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

# SUMMARY_PROMPT caps the briefing at 400 words. At ~6 chars/word (5-letter
# average English word + a space) that's ~2400 chars, and estimate_tokens is
# chars // 4, so a from-scratch summary costs on the order of 600 tokens even
# though there's no previous frame yet to measure it from. Without this, a
# conversation's first-ever compaction computes frame_cost=0 (nothing to
# diff against), under-reserves for the summary about to be written, and can
# land the post-compaction context back over budget by however big that
# frame turns out to be.
FIRST_SUMMARY_RESERVE_TOKENS = 600

# `target` must stay above zero even when frame_cost eats the whole budget
# (e.g. a pathologically large stored summary at a small configured budget).
# At target == 0, _cut_index can essentially never find a fitting
# user-boundary suffix, so `cut` stays 0 forever and compaction permanently
# stops advancing while the context keeps growing — not reachable at the
# 60k default, but a smaller configured budget shouldn't be able to deadlock
# compaction outright. This is a floor, not a guarantee a cut is found.
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
        rather than something each call site has to remember to uphold.
        """
        previous, since = self.store.summary(conversation_id)
        framed = self._frame(previous, history)
        if estimate_tokens(framed) <= self.budget_tokens:
            return framed, history

        # The overflow check above counts the leading summary frame's tokens
        # toward the budget (it's part of `framed`), but a plain cut over
        # `history` doesn't know that frame exists — so without this, the
        # post-compaction context (new frame + newer) could land back over
        # `target` by however big the summary frame is. Reserving that same
        # cost here (using the current frame as a proxy for the new one, since
        # both are bounded by the same summarization prompt) keeps the two
        # checks measuring the same thing. When there's no previous summary
        # yet, that diff is always 0 (there's no frame to measure), so fall
        # back to a fixed reserve for the summary about to be written instead.
        frame_cost = (estimate_tokens(framed) - estimate_tokens(history)
                      if previous.strip() else FIRST_SUMMARY_RESERVE_TOKENS)
        target = max((self.budget_tokens * 2) // 3 - frame_cost, MIN_CUT_TARGET_TOKENS)
        cut = _cut_index(history, target)
        if cut == 0:
            return framed, history  # nothing can be dropped without splitting a tool call

        # `cut` is a local index into `history`, which by the docstring's
        # invariant is exactly the transcript from `since` forward — so
        # `turn_ids` (fetched fresh from that same `since`) must be the same
        # length as `history`. That invariant can break for two different
        # reasons: a caller failed to adopt the trimmed history from a
        # previous call (a bug), or the store has legitimately fallen behind
        # because writes are failing (AgentSession._append degrades to an
        # in-memory session on a persistence failure rather than ending the
        # conversation — see its docstring). Both look identical from here:
        # `history` keeps growing while the store doesn't. We can't reliably
        # tell them apart, and a live chat dying mid-turn is a far worse
        # outcome than silently not-yet-summarizing a turn, so either way the
        # answer is the same as an LLM failure — degrade to the
        # oversized-but-correct context and advance nothing. This check runs
        # before any side-effecting work (the flush hook, the summarizing LLM
        # call) so that a misalignment is caught before either fires, not
        # after both have already run for nothing. It's a plain branch, not
        # an assert, so it still holds under `python -O`.
        turn_ids = [tid for tid, _ in
                    self.store.history_with_ids(conversation_id, after_turn_id=since)]
        if len(turn_ids) != len(history):
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
        self.store.set_summary(conversation_id, summary, through)
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
