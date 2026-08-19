from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.compact import Compactor
from allpath_trade.agent.context import build_system_prompt, load_identity
from allpath_trade.agent.loop import AgentSession
from allpath_trade.agent.memory_tools import register_memory_tools
from allpath_trade.agent.readonly_tools import register_readonly_tools
from allpath_trade.agent.tools import ToolRegistry, fence_external
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
        self._mirror: Callable[[str, str, str], None] | None = None

    def _stale(self) -> bool:
        age = datetime.now(UTC).timestamp() - self._built_at
        return age > SNAPSHOT_TTL_SECONDS

    def invalidate(self) -> None:
        """Discard the cached session so the next `session()` call rebuilds
        via `_build()`, which re-resolves everything (LLM client, tools,
        prompt) through `self.holder.get()` -- picking up whatever Components
        a settings save just installed.

        This object is now constructed once at app startup and shared with
        the Telegram poller (Task 3), so a settings save can no longer just
        replace `app.state.chat_service` with a fresh instance the way the
        old lazy-`_service()` reset did (`request.app.state.chat = None`) --
        that would leave the poller holding a stale, orphaned ChatService
        while the web route got a new one, splitting the shared turn lock
        and conversation the whole hoist-to-startup design exists to keep
        unified. Invalidating just the cached session preserves identity
        while still forcing the next turn to pick up new config."""
        with self._lock:
            self._session = None

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
            order_sink=QueueingOrderSink(
                c.queue, c.gate, c.broker, c.data, c.journal, conversation_id,
                notifier=c.notifier, app_state=c.app_state,
                telegram_bot_token=c.settings.telegram_bot_token,
                web_base_url=c.settings.web_base_url),
            # `queue` (not `order_sink`) is draft_strategy's own web-mode
            # discriminator -- see action_tools.py's register_action_tools
            # docstring comment for why the two tools don't share one
            # signal. `conversation_id` is the same stable per-session id
            # order_sink above was built with (store.latest()/start() a few
            # lines up); this is the ChatService's ONE per-request build
            # site, so passing it directly here is as fresh as a callable
            # indirection would be, with none of the extra machinery.
            queue=c.queue, conversation_id=conversation_id,
            notifier=c.notifier, web_base_url=c.settings.web_base_url,
            app_state=c.app_state, telegram_bot_token=c.settings.telegram_bot_token)

        prompt = build_system_prompt(
            identity=load_identity(), broker=c.broker, journal=c.journal,
            strategies=c.strategies, queue=c.queue, memory=c.memory)
        # Finding 8: flush durable preferences to curated memory before the
        # older half of the conversation is dropped from context (see
        # Compactor's docstring on on_before_compact). Under Phase 5's
        # one-conversation-forever design there is no "end of chat" here to
        # hang consolidation off of the way cli.py's cmd_chat does -- this
        # hook is the only backstop against losing a preference the user
        # stated once and never repeated. No-op when no LLM is configured
        # (c.consolidator is None in that case).
        # F2: `propagate=True` -- see cli.py's cmd_chat for why. Without it,
        # a failed flush here reads to Compactor as an ordinary success and
        # the older messages get summarized and dropped right after the
        # flush meant to preserve them just failed.
        compactor = Compactor(
            build_llm(c.settings, tier="memory", usage_store=c.llm_usage), store,
            budget_tokens=c.settings.context_budget_tokens,
            on_before_compact=(
                partial(c.consolidator.run_post_chat, propagate=True)
                if c.consolidator is not None else None))
        return AgentSession(
            build_llm(c.settings, tier="chat", usage_store=c.llm_usage),
            registry, prompt, store=store, conversation_id=conversation_id,
            compactor=compactor,
            on_tool=lambda call: self.activity.append(call.name))

    def send(self, text: str, source: str = "web") -> str:
        with self._turn_lock:
            self.activity = []
            reply = self.session().run_turn(text, extra={"source": source})
        # Mirroring happens after `_turn_lock` is released (the `with` block
        # above has already exited) -- Task 5's mirror fn does its own
        # thread-pool submit for the actual Telegram HTTP call, but even the
        # synchronous dispatch here must not extend the lock hold, or a
        # slow/hanging mirror would add latency to every web chat turn and
        # block the Telegram poller's own next `send()` behind it.
        self._call_mirror(source, text, reply)
        return reply

    def set_mirror(self, fn: Callable[[str, str, str], None] | None) -> None:
        """Registers the hook `send`/`note_resolution` call after a turn
        completes, as `fn(source, user_text, reply)`. Direction (push to
        Telegram only for web-sourced turns) is Task 5's `_mirror_to_telegram`
        policy, not this class's concern -- ChatService just fires the hook
        for every source and lets the fn decide. No mirror registered (the
        default) means zero behavior change from pre-Task-4 ChatService.

        `fn=None` clears a previously registered hook -- `_stop_telegram`
        (Finding 3) calls this on shutdown so a mirror hook never outlives
        the executor/queue it was closed over."""
        self._mirror = fn

    def _call_mirror(self, source: str, text: str, reply: str) -> None:
        if self._mirror is None:
            return
        try:
            self._mirror(source, text, reply)
        except Exception as exc:  # noqa: BLE001 — a mirror failure must never
            # surface as a broken chat turn; the turn already completed and
            # the reply already went to the caller by the time this runs.
            print(f"[warning] chat mirror failed: {exc}", file=sys.stderr)

    def messages(self) -> list[dict]:
        return list(self.session().history)

    def note_resolution(self, line: str, source: str = "web") -> None:
        """Record an out-of-band event (an approval, a fill) in the transcript
        so the agent sees it on its next turn.

        `line` (built in reviews.py's `_echo_resolution`) can carry a
        user-supplied reject note or a raw broker exception message -- text
        this process didn't originate and can't fully trust. A bare
        `"[system] "` text prefix would itself be exactly the kind of
        forgeable marker that matters here: a `[system]`-looking line
        smuggled inside a reject note would read to the model as a second,
        indistinguishable system event. `fence_external` is the same
        machinery already used to
        wall off tool output and consolidation transcripts (readonly_tools.py,
        memory/consolidate.py) -- it neutralizes any embedded fence-breakout
        attempt and tells the model not to treat the contents as instructions,
        so a forged marker inside `line` can't impersonate a real one. The
        unfenced `line` is kept as `display` purely for the human-facing
        transcript (see _chat_messages.html), which renders it distinctly
        from a message the user actually typed rather than showing the raw
        fence.

        Shares `_turn_lock` with `send()`: an approval clicked mid-turn must
        not append into the history while `run_turn` is iterating over it.
        That does mean an approve/reject click during a long-running turn
        waits for the whole turn to finish before the redirect returns --
        state is safe either way (the queue row is committed before this is
        ever called), but the browser hangs for the duration. Left as is:
        the alternative (an out-of-band queue flushed independently of
        `_turn_lock`) trades a UI stall for new interleaving cases this
        module was written specifically to rule out."""
        with self._turn_lock:
            session = self.session()
            session._append({"role": "user", "content": fence_external(line),
                             "kind": "system_note", "display": line})
        # An approval receipt is part of the full record the user chose to
        # mirror (spec §④) -- same hook as send(), fired after the lock.
        # `source` defaults to "web" (the web reviews flow, the original and
        # still most common caller) but a Telegram button-tap resolution
        # (telegram.py's `_resolve_review_callback`) passes source="telegram"
        # so the mirror's direction policy (`_mirror_to_telegram`, which
        # no-ops for source != "web") doesn't push a second, redundant copy
        # of the outcome back into the same Telegram chat that already got
        # the immediate in-channel tap feedback. No agent reply accompanies
        # a resolution note, so `reply` is the empty string; the mirror fn
        # decides what (if anything) to do with that.
        self._call_mirror(source, line, "")
