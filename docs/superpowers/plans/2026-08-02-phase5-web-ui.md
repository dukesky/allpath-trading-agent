# Phase 5: Web UI + Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a local web interface that becomes the primary way the user talks to the agent, reviews pending trades, and configures the system — with email notifications wired for real use.

**Architecture:** One process (`allpath-trade serve`) runs a FastAPI app and the sentinel scheduler together. Pages are server-rendered Jinja2 templates with htmx for partial updates — no build step, no npm. Every page reuses the existing service objects from `build_components()`; the web layer never touches `Executor` directly. Agent-proposed orders go into the existing `pending_reviews` queue instead of blocking on a synchronous confirm, so a closed tab never loses a proposal.

**Tech Stack:** Python ≥3.11, FastAPI, uvicorn, Jinja2, htmx (vendored), SQLite (WAL), APScheduler, pytest.

## Global Constraints

- **All UI text is English.** Page labels, buttons, CLI output, email bodies, error messages. The user may write in any language and the agent replies in kind, but nothing in a template or a notification body is Chinese.
- **The web layer never calls `Executor.execute()`.** The only paths to the broker are `ReviewQueue.approve()` and `Sentinel`. This is the money-path invariant from Phase 1 and it must survive Phase 5.
- **Secrets are write-only.** Once stored, a secret value is never rendered back into a response body. Templates show a mask plus a "Replace" control.
- **No new frontend build step.** htmx is a vendored `.js` file in `allpath_trade/web/static/`. No npm, no bundler, no CDN reference (the app must work offline).
- Line length 100 (ruff). Run `uv run ruff check .` before every commit.
- Every task ends green: `uv run pytest -q` passes.

---

## File Structure

**New package `allpath_trade/web/`:**
- `__init__.py` — empty
- `app.py` — FastAPI app factory, lifespan (scheduler start/stop), component holder
- `auth.py` — token generation, login/logout routes, auth + Origin middleware
- `deps.py` — request-scoped accessors for the component graph
- `format.py` — display helpers used by templates (money, percent, relative time)
- `routes/dashboard.py`, `routes/chat.py`, `routes/reviews.py`, `routes/strategies.py`, `routes/memory.py`, `routes/settings.py`
- `templates/` — `base.html`, `login.html`, `dashboard.html`, `chat.html`, `_chat_messages.html`, `reviews.html`, `_review_card.html`, `strategies.html`, `strategy_detail.html`, `memory.html`, `settings.html`
- `static/app.css`, `static/htmx.min.js`

**New elsewhere:**
- `allpath_trade/agent/compact.py` — context-window compaction
- `allpath_trade/notify/events.py` — English notification bodies for the four events

**Modified:**
- `allpath_trade/config.py` — `set()` quoting, `web_*` settings, `context_budget_tokens`, consolidation toggles
- `allpath_trade/store/db.py` — WAL, busy timeout, thread-safe connection, new migrations
- `allpath_trade/store/conversations.py` — rolling summary support
- `allpath_trade/store/reviews.py` — `add()` gains `source` / `conversation_id`, `list()` filters
- `allpath_trade/agent/loop.py` — compaction hook
- `allpath_trade/agent/action_tools.py` — pluggable order sink
- `allpath_trade/memory/store.py` — `MemoryError` → `MemoryStoreError`
- `allpath_trade/cli.py` — `serve` command
- `allpath_trade/sentinel.py` — emit notification events
- `pyproject.toml`, `.env.example`, `README.md`, `README.zh-CN.md`, `docs/TODO.md`

---

### Task 1: Prerequisites — settings quoting and the `MemoryError` rename

Two small blockers the web layer would trip over. `SettingsStore.set` writes `.env` with `quote_mode="never"`, which corrupts any value containing a space, `#`, or `=` — and the settings page will write arbitrary values. `memory/store.py` defines a class named `MemoryError`, shadowing the builtin.

**Files:**
- Modify: `allpath_trade/config.py:47-49`
- Modify: `allpath_trade/memory/store.py` (class name and all raise sites)
- Modify: `allpath_trade/agent/memory_tools.py`, `allpath_trade/memory/consolidate.py` (importers)
- Test: `tests/test_config.py`, `tests/test_memory_store.py`

**Interfaces:**
- Consumes: nothing
- Produces: `SettingsStore.set(key, value)` safe for arbitrary values; `allpath_trade.memory.store.MemoryStoreError` (with `MemoryError` retained as a module-level alias for compatibility)

- [ ] **Step 1: Write the failing round-trip test**

Add to `tests/test_config.py`:

```python
def test_set_preserves_values_with_spaces_hashes_and_equals(tmp_path):
    store = SettingsStore(tmp_path / ".env")
    store.set("SMTP_FROM", "AllPath Trade <bot@example.com>")
    store.set("WEB_TOKEN", "abc#def=ghi jkl")
    reloaded = SettingsStore(tmp_path / ".env")
    assert reloaded.get("SMTP_FROM") == "AllPath Trade <bot@example.com>"
    assert reloaded.get("WEB_TOKEN") == "abc#def=ghi jkl"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_config.py::test_set_preserves_values_with_spaces_hashes_and_equals -v`
Expected: FAIL — the `#` truncates the value on read-back.

- [ ] **Step 3: Fix the quoting**

In `allpath_trade/config.py`, replace the body of `set`:

```python
    def set(self, key: str, value: str) -> None:
        # quote_mode="always": values reach here from the settings page and may
        # contain spaces, '#', or '=' — unquoted, dotenv truncates or mangles them.
        self.env_file.touch(exist_ok=True)
        set_key(str(self.env_file), key, value, quote_mode="always")
```

- [ ] **Step 4: Run it and watch it pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Rename the exception**

In `allpath_trade/memory/store.py`, rename the class and add the alias at the end of the class block:

```python
class MemoryStoreError(Exception):
    """Raised for invalid layers, keys, actions, or budget overruns."""


# Backwards-compatible alias for the pre-Phase-5 name.
MemoryError = MemoryStoreError  # noqa: A001 — retained for import compatibility
```

Then replace every `raise MemoryError(` in that file with `raise MemoryStoreError(`. Update the imports and `except` clauses in `allpath_trade/agent/memory_tools.py` and `allpath_trade/memory/consolidate.py` to use `MemoryStoreError`.

Run: `uv run grep -rn "MemoryError" allpath_trade/` — the only remaining hit should be the alias line.

- [ ] **Step 6: Add a test that both names work**

Add to `tests/test_memory_store.py`:

```python
def test_memory_store_error_is_exported_under_both_names():
    from allpath_trade.memory.store import MemoryError, MemoryStoreError

    assert MemoryError is MemoryStoreError
    assert issubclass(MemoryStoreError, Exception)
```

- [ ] **Step 7: Full suite and lint**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "fix: quote .env values on write; rename MemoryError to MemoryStoreError"
```

---

### Task 2: Thread-safe database access

`serve` runs the web app and the sentinel scheduler in one process. Today `connect()` hands out a single connection with `check_same_thread=False` and relies on there being exactly one writer. Two writers are about to exist.

We serialize access through a lock rather than handing each thread its own connection: every store in the codebase already takes a `conn` object, single-statement-then-commit is the universal write pattern here, and a single-user app gains nothing from write concurrency. WAL mode plus a busy timeout covers the reader side.

**Files:**
- Modify: `allpath_trade/store/db.py`
- Test: `tests/test_db_concurrency.py` (create)

**Interfaces:**
- Consumes: nothing
- Produces: `connect(path)` returns a `LockedConnection` exposing `execute`, `executemany`, `executescript`, `commit`, `close`, `row_factory`, and `__enter__`/`__exit__` — a drop-in for the `sqlite3.Connection` every store already accepts.

- [ ] **Step 1: Write the failing concurrency test**

Create `tests/test_db_concurrency.py`:

```python
import threading

from allpath_trade.store.db import connect


def test_concurrent_writers_do_not_lose_rows(tmp_path):
    conn = connect(tmp_path / "t.db")
    errors: list[Exception] = []

    def writer(tag: str) -> None:
        try:
            for i in range(50):
                conn.execute(
                    "INSERT INTO observations (ts, source, subject, text)"
                    " VALUES ('t', ?, NULL, ?)", (tag, str(i)))
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    count = conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
    assert count == 200


def test_wal_mode_is_enabled(tmp_path):
    conn = connect(tmp_path / "t.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_db_concurrency.py -v`
Expected: `test_wal_mode_is_enabled` FAILS (journal mode is `delete`). The concurrency test may pass by luck — that is exactly why the lock is worth making explicit.

- [ ] **Step 3: Implement the locked connection**

In `allpath_trade/store/db.py`, add above `connect`:

```python
class LockedConnection:
    """Serializes access to one sqlite connection.

    `serve` runs the web app and the sentinel scheduler in a single process,
    so two threads write to this database. Every store in the codebase takes
    a connection object and writes with a single statement followed by a
    commit, so one lock around the connection is both sufficient and cheaper
    than threading a connection pool through every constructor. A single-user
    app has nothing to gain from write concurrency."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    @property
    def row_factory(self):  # noqa: ANN201 — mirrors sqlite3.Connection
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:  # noqa: ANN001
        self._conn.row_factory = value

    def execute(self, sql: str, parameters=()):  # noqa: ANN001, ANN201
        with self._lock:
            cur = self._conn.execute(sql, parameters)
            # Materialize now: the caller may iterate the cursor after another
            # thread has taken the lock and started writing.
            return _Rows(cur)

    def executemany(self, sql: str, seq):  # noqa: ANN001, ANN201
        with self._lock:
            return _Rows(self._conn.executemany(sql, seq))

    def executescript(self, script: str):  # noqa: ANN201
        with self._lock:
            return _Rows(self._conn.executescript(script))

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Rows:
    """A materialized cursor: rows are read eagerly, `lastrowid`/`rowcount`
    are captured, so nothing depends on the cursor staying valid."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        try:
            self._rows = cursor.fetchall()
        except sqlite3.ProgrammingError:
            self._rows = []  # statement returned no result set

    def fetchone(self):  # noqa: ANN201
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)

    def __iter__(self):  # noqa: ANN204
        return iter(self._rows)
```

Add `import threading` at the top of the file. Then rewrite `connect`:

```python
def connect(path: Path | str) -> LockedConnection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    locked = LockedConnection(conn)
    locked.executescript(SCHEMA)
    _migrate(locked)
    return locked
```

Change `_migrate`'s annotation to `conn: LockedConnection`.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. If any test fails because it called a cursor method `_Rows` does not expose, add that method to `_Rows` rather than reaching for the raw connection.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add -A
git commit -m "fix: serialize sqlite access and enable WAL for the serve process"
```

---

### Task 3: Rolling context compaction

The user never manages sessions — no new-chat button, no session list. So the system must keep one ever-growing conversation inside a bounded context. `ConversationStore.history()` currently loads every turn ever recorded.

**Files:**
- Create: `allpath_trade/agent/compact.py`
- Create: `tests/test_compact.py`
- Modify: `allpath_trade/store/db.py` (migrations), `allpath_trade/store/conversations.py`, `allpath_trade/agent/loop.py`, `allpath_trade/config.py`

**Interfaces:**
- Consumes: `LockedConnection` (Task 2), `LLMClient.complete(messages, tools=None) -> LLMResponse`
- Produces:
  - `ConversationStore.summary(conversation_id) -> tuple[str, int]` returning `(summary_text, summarized_through_turn_id)`
  - `ConversationStore.set_summary(conversation_id, text, through_turn_id) -> None`
  - `ConversationStore.history(conversation_id, after_turn_id: int = 0) -> list[dict]`
  - `estimate_tokens(messages: list[dict]) -> int`
  - `Compactor(llm, store, on_before_compact=None).maybe_compact(conversation_id, history) -> list[dict]`
  - `Settings.context_budget_tokens: int = 60000`

- [ ] **Step 1: Add the schema columns**

In `allpath_trade/store/db.py`, extend `_MIGRATIONS`:

```python
_MIGRATIONS = [
    "ALTER TABLE pending_reviews ADD COLUMN agent_analysis TEXT",
    "ALTER TABLE conversations ADD COLUMN summary TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN summarized_through INTEGER NOT NULL DEFAULT 0",
]
```

- [ ] **Step 2: Write the failing store test**

Create `tests/test_compact.py`:

```python
from allpath_trade.agent.compact import Compactor, estimate_tokens
from allpath_trade.llm.base import LLMResponse
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect
from tests.test_agent_loop import ScriptedLLM


def store(tmp_path) -> ConversationStore:
    return ConversationStore(connect(tmp_path / "t.db"))


def test_history_can_start_after_a_turn_id(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for i in range(4):
        s.append(cid, {"role": "user", "content": f"m{i}"})
    all_turns = s.history_with_ids(cid)
    cutoff = all_turns[1][0]
    tail = s.history(cid, after_turn_id=cutoff)
    assert [m["content"] for m in tail] == ["m2", "m3"]


def test_summary_round_trips(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    s.set_summary(cid, "user prefers dividends", 7)
    assert s.summary(cid) == ("user prefers dividends", 7)
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_compact.py -v`
Expected: FAIL — `history_with_ids`, `summary`, `set_summary` do not exist.

- [ ] **Step 4: Extend `ConversationStore`**

In `allpath_trade/store/conversations.py`, replace `history` and add the new methods:

```python
    def history(self, conversation_id: int, after_turn_id: int = 0) -> list[dict]:
        rows = self._conn.execute(
            "SELECT message FROM conversation_turns"
            " WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, after_turn_id))
        return [json.loads(r["message"]) for r in rows]

    def history_with_ids(self, conversation_id: int,
                         after_turn_id: int = 0) -> list[tuple[int, dict]]:
        rows = self._conn.execute(
            "SELECT id, message FROM conversation_turns"
            " WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, after_turn_id))
        return [(r["id"], json.loads(r["message"])) for r in rows]

    def summary(self, conversation_id: int) -> tuple[str, int]:
        row = self._conn.execute(
            "SELECT summary, summarized_through FROM conversations WHERE id = ?",
            (conversation_id,)).fetchone()
        if row is None:
            return "", 0
        return row["summary"], row["summarized_through"]

    def set_summary(self, conversation_id: int, text: str,
                    through_turn_id: int) -> None:
        self._conn.execute(
            "UPDATE conversations SET summary = ?, summarized_through = ?"
            " WHERE id = ?", (text, through_turn_id, conversation_id))
        self._conn.commit()
```

- [ ] **Step 5: Run the store tests**

Run: `uv run pytest tests/test_compact.py -v`
Expected: PASS

- [ ] **Step 6: Write the failing compactor tests**

Append to `tests/test_compact.py`:

```python
def big(role: str, n: int) -> dict:
    return {"role": role, "content": "x" * n}


def test_estimate_tokens_scales_with_content():
    assert estimate_tokens([big("user", 4000)]) > 900


def test_no_compaction_under_budget(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    llm = ScriptedLLM([])
    c = Compactor(llm, s, budget_tokens=10_000)
    history = [big("user", 100), big("assistant", 100)]
    assert c.maybe_compact(cid, history) == history


def test_compaction_summarizes_the_oldest_messages(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(10):
        s.append(cid, big("user", 2000))
        s.append(cid, big("assistant", 2000))
    llm = ScriptedLLM([LLMResponse(text="earlier: the user asked about NVDA")])
    c = Compactor(llm, s, budget_tokens=2_000)
    history = s.history(cid)

    result = c.maybe_compact(cid, history)

    assert len(result) < len(history)
    assert result[0]["role"] == "system"
    assert "NVDA" in result[0]["content"]
    assert s.summary(cid)[1] > 0


def test_compaction_never_splits_a_tool_call_from_its_result(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    s.append(cid, big("user", 3000))
    s.append(cid, {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c1", "name": "quote", "arguments": {}}]})
    s.append(cid, {"role": "tool", "tool_call_id": "c1", "content": "199.0"})
    s.append(cid, big("assistant", 3000))
    s.append(cid, big("user", 100))
    llm = ScriptedLLM([LLMResponse(text="summary")])
    c = Compactor(llm, s, budget_tokens=500)

    result = c.maybe_compact(cid, s.history(cid))

    kept = [m for m in result if m["role"] != "system"]
    ids = {m["tool_call_id"] for m in kept if m["role"] == "tool"}
    called = {call["id"] for m in kept for call in m.get("tool_calls", [])}
    assert ids == called


def test_flush_hook_runs_before_summarizing(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(6):
        s.append(cid, big("user", 3000))
        s.append(cid, big("assistant", 100))
    order: list[str] = []
    llm = ScriptedLLM([LLMResponse(text="summary")])
    c = Compactor(llm, s, budget_tokens=500,
                  on_before_compact=lambda msgs: order.append("flush"))
    c.maybe_compact(cid, s.history(cid))
    assert order == ["flush"]


def test_llm_failure_leaves_history_untouched(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(6):
        s.append(cid, big("user", 3000))
    history = s.history(cid)
    c = Compactor(FailingLLM(), s, budget_tokens=500)
    assert c.maybe_compact(cid, history) == history
    assert s.summary(cid) == ("", 0)
```

Add the failing client at the top of the file:

```python
from allpath_trade.llm.base import LLMError


class FailingLLM:
    def complete(self, messages, tools=None):  # noqa: ANN001, ANN201, ARG002
        raise LLMError("boom")
```

- [ ] **Step 7: Run and watch them fail**

Run: `uv run pytest tests/test_compact.py -v`
Expected: FAIL — `allpath_trade.agent.compact` does not exist.

- [ ] **Step 8: Implement the compactor**

Create `allpath_trade/agent/compact.py`:

```python
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
        previous, _ = self.store.summary(conversation_id)
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

        turn_ids = [tid for tid, _ in
                    self.store.history_with_ids(conversation_id)][:cut]
        through = turn_ids[-1] if turn_ids else 0
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
```

- [ ] **Step 9: Run the compactor tests**

Run: `uv run pytest tests/test_compact.py -v`
Expected: PASS

- [ ] **Step 10: Wire the compactor into `AgentSession`**

In `allpath_trade/agent/loop.py`, add a `compactor` parameter and apply it when assembling messages:

```python
    def __init__(self, llm: LLMClient, registry: ToolRegistry, system_prompt: str,
                 store: ConversationStore | None = None,
                 conversation_id: int | None = None, max_iters: int = 15,
                 on_tool: Callable[[ToolCall], None] | None = None,
                 compactor: object | None = None) -> None:
```

Set `self.compactor = compactor` alongside the other assignments, then load the history through the summary marker:

```python
        if store is not None and conversation_id is not None:
            _, through = store.summary(conversation_id)
            self.history = store.history(conversation_id, after_turn_id=through)
```

And in `run_turn`, replace the message assembly:

```python
            context = self.history
            if self.compactor is not None and self.conversation_id is not None:
                context = self.compactor.maybe_compact(
                    self.conversation_id, self.history)
            messages = [{"role": "system", "content": self.system_prompt}, *context]
```

- [ ] **Step 11: Add the integration test**

Append to `tests/test_compact.py`:

```python
def test_session_resumes_from_the_summary_marker(tmp_path):
    from allpath_trade.agent.loop import AgentSession
    from allpath_trade.agent.tools import ToolRegistry

    s = store(tmp_path)
    cid = s.start()
    s.append(cid, {"role": "user", "content": "old and forgotten"})
    ids = [tid for tid, _ in s.history_with_ids(cid)]
    s.set_summary(cid, "briefing", ids[-1])
    s.append(cid, {"role": "user", "content": "still visible"})

    session = AgentSession(ScriptedLLM([]), ToolRegistry(), "sys",
                           store=s, conversation_id=cid)
    assert [m["content"] for m in session.history] == ["still visible"]
```

- [ ] **Step 12: Add the setting**

In `allpath_trade/config.py`, add to `Settings`:

```python
    context_budget_tokens: int = 60000
```

- [ ] **Step 13: Full suite, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: rolling context compaction so one conversation can run forever"
```

---

### Task 4: The `serve` process

**Files:**
- Create: `allpath_trade/web/__init__.py`, `allpath_trade/web/app.py`, `allpath_trade/web/deps.py`
- Create: `tests/test_web_app.py`
- Modify: `pyproject.toml`, `allpath_trade/config.py`, `allpath_trade/cli.py`, `.env.example`

**Interfaces:**
- Consumes: `build_components(settings, broker=None) -> Components`
- Produces:
  - `create_app(settings, broker=None, llm_factory=None) -> FastAPI` with `app.state.holder`
  - `ComponentHolder.get() -> Components` and `ComponentHolder.rebuild() -> None`
  - `Settings.web_host: str = "127.0.0.1"`, `Settings.web_port: int = 8791`
  - CLI: `allpath-trade serve [--host H] [--port P]`

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add to `dependencies`:

```toml
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",
```

and to the dev group: `"httpx>=0.27"` (required by `fastapi.testclient`).

Run: `uv sync`

- [ ] **Step 2: Write the failing app test**

Create `tests/test_web_app.py`:

```python
import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    app = create_app(settings, broker=FakeBroker())
    with TestClient(app) as c:
        yield c


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_components_are_available_on_the_app(client):
    holder = client.app.state.holder
    assert holder.get().broker is not None
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_web_app.py -v`
Expected: FAIL — module `allpath_trade.web.app` not found.

- [ ] **Step 4: Add the settings**

In `allpath_trade/config.py`, add to `Settings`:

```python
    web_host: str = "127.0.0.1"
    web_port: int = 8791
    web_token: str = ""
    daily_consolidation: bool = True
    consolidate_after_chat: bool = True
```

- [ ] **Step 5: Write the component holder**

Create `allpath_trade/web/deps.py`:

```python
from __future__ import annotations

import threading
from collections.abc import Callable

from allpath_trade.app import Components, build_components
from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings, SettingsStore


class ComponentHolder:
    """Owns the component graph for the life of the process.

    The settings page can rewrite `.env` at any time, so the graph is
    rebuildable: `rebuild()` swaps in a fresh one and new requests pick it up.
    Conversations already in flight keep the objects they captured."""

    def __init__(self, settings: Settings, broker: Broker | None = None,
                 builder: Callable[[Settings, Broker | None], Components] | None = None,
                 env_file: str = ".env") -> None:
        self._broker = broker
        self._builder = builder or build_components
        self._store = SettingsStore(env_file)
        self._lock = threading.Lock()
        self._components = self._builder(settings, broker)

    def get(self) -> Components:
        with self._lock:
            return self._components

    def settings(self) -> Settings:
        return self.get().settings

    def rebuild(self, settings: Settings | None = None) -> None:
        fresh = settings or self._store.load()
        built = self._builder(fresh, self._broker)
        with self._lock:
            self._components = built


def holder(request) -> ComponentHolder:  # noqa: ANN001 — FastAPI Request
    return request.app.state.holder


def components(request) -> Components:  # noqa: ANN001
    return request.app.state.holder.get()
```

- [ ] **Step 6: Write the app factory**

Create `allpath_trade/web/app.py`:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.web.deps import ComponentHolder

STATIC_DIR = Path(__file__).parent / "static"


def _start_scheduler(app: FastAPI) -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    from allpath_trade.scheduler import build_jobs

    holder = app.state.holder
    scheduler = BackgroundScheduler()
    build_jobs(scheduler, holder)
    scheduler.start()
    app.state.scheduler = scheduler


def create_app(settings: Settings, broker: Broker | None = None,
               start_scheduler: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        if start_scheduler:
            _start_scheduler(app)
        yield
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="AllPath Trade", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.holder = ComponentHolder(settings, broker)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
```

`docs_url`/`redoc_url`/`openapi_url` are disabled deliberately: this app is reachable from the LAN and there is no reason to publish a machine-readable map of its routes.

- [ ] **Step 7: Add the scheduler job builder**

In `allpath_trade/scheduler.py`, add below `run_daemon`:

```python
def build_jobs(scheduler, holder) -> None:  # noqa: ANN001
    """Attach the sentinel and the after-close consolidation to a scheduler
    owned by someone else (the `serve` process)."""
    state = {"last_daily": None}

    def job() -> None:
        components = holder.get()
        if is_market_hours():
            components.sentinel.run_once()
        consolidator = components.consolidator
        if (consolidator is not None
                and components.settings.daily_consolidation
                and _is_after_close()):
            today = datetime.now(UTC).astimezone(ET).date().isoformat()
            if state["last_daily"] != today:
                state["last_daily"] = today
                try:
                    consolidator.run_daily()
                except Exception as exc:  # noqa: BLE001 — a failed digest must not stop the loop
                    print(f"[daily] failed: {exc}")

    scheduler.add_job(job, "interval",
                      minutes=holder.settings().sentinel_interval_minutes,
                      next_run_time=datetime.now(UTC))
```

- [ ] **Step 8: Add the `serve` CLI command**

In `allpath_trade/cli.py`, register the subparser alongside the others:

```python
    p_serve = sub.add_parser("serve", help="run the web interface and sentinel")
    p_serve.add_argument("--host", default=None, help="bind address")
    p_serve.add_argument("--port", type=int, default=None, help="port")
```

and handle it in `main` before the broker-dependent commands are dispatched:

```python
    if args.command == "serve":
        return cmd_serve(settings, args.host, args.port)
```

Add `cmd_serve`:

```python
def cmd_serve(settings: Settings, host: str | None, port: int | None) -> int:
    import uvicorn

    from allpath_trade.web.app import create_app

    host = host or settings.web_host
    port = port or settings.web_port
    app = create_app(settings, start_scheduler=True)
    shown = "localhost" if host in {"127.0.0.1", "localhost"} else host
    print(f"[allpath-trade] http://{shown}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
```

Add `"serve"` to the `needs_broker` set so credentials are validated before the server starts.

- [ ] **Step 9: Update `.env.example`**

```
# Web interface
WEB_HOST=127.0.0.1
WEB_PORT=8791
WEB_TOKEN=
CONTEXT_BUDGET_TOKENS=60000
DAILY_CONSOLIDATION=true
CONSOLIDATE_AFTER_CHAT=true
```

- [ ] **Step 10: Run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: allpath-trade serve — FastAPI app with the sentinel in-process"
```

---

### Task 5: Token authentication

The server binds to the LAN, so anything that can reach the port can otherwise place orders.

**Files:**
- Create: `allpath_trade/web/auth.py`, `allpath_trade/web/templates/login.html`
- Modify: `allpath_trade/web/app.py`
- Test: `tests/test_web_auth.py` (create)

**Interfaces:**
- Consumes: `ComponentHolder` (Task 4)
- Produces: `install_auth(app)`, `ensure_token(settings_store, settings) -> str`, and `templates` (a shared `Jinja2Templates` instance importable as `allpath_trade.web.templating.templates`)

- [ ] **Step 1: Write the failing auth tests**

Create `tests/test_web_auth.py`:

```python
import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        yield c


def test_anonymous_request_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_with_the_wrong_token_is_rejected(client):
    r = client.post("/login", data={"token": "nope"}, follow_redirects=False)
    assert r.status_code == 401
    assert "allpath_session" not in r.cookies


def test_login_then_browse(client):
    r = client.post("/login", data={"token": "secret"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_cross_origin_post_is_rejected(client):
    client.post("/login", data={"token": "secret"})
    r = client.post("/reviews/1/reject", data={},
                    headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_static_assets_need_no_auth(client):
    assert client.get("/static/app.css").status_code in (200, 404)
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: FAIL — no `/login` route.

- [ ] **Step 3: Create the shared templates object**

Create `allpath_trade/web/templating.py`:

```python
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from allpath_trade.web import format as fmt

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = fmt.money
templates.env.filters["pct"] = fmt.pct
templates.env.filters["ago"] = fmt.ago
```

Create `allpath_trade/web/format.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal


def money(value) -> str:  # noqa: ANN001
    try:
        return f"${Decimal(str(value)):,.2f}"
    except Exception:  # noqa: BLE001 — templates must never raise
        return "—"


def pct(value) -> str:  # noqa: ANN001
    try:
        return f"{Decimal(str(value)) * 100:+.2f}%"
    except Exception:  # noqa: BLE001
        return "—"


def ago(ts: str) -> str:
    try:
        then = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ts or ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
```

- [ ] **Step 4: Implement auth**

Create `allpath_trade/web/auth.py`:

```python
from __future__ import annotations

import hmac
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.config import Settings, SettingsStore
from allpath_trade.web.templating import templates

COOKIE = "allpath_session"
REMEMBER_SECONDS = 30 * 24 * 3600
PUBLIC_PATHS = {"/login", "/healthz"}


def ensure_token(store: SettingsStore, settings: Settings) -> str:
    """Return the access token, generating and persisting one on first run."""
    if settings.web_token:
        return settings.web_token
    token = secrets.token_urlsafe(24)
    store.set("WEB_TOKEN", token)
    settings.web_token = token
    return token


def _authorized(request: Request, token: str) -> bool:
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, token)


def install_auth(app: FastAPI) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        token = request.app.state.holder.settings().web_token
        if not _authorized(request, token):
            return RedirectResponse("/login", status_code=303)

        if request.method not in ("GET", "HEAD"):
            # Same-origin check: on a LAN, another device could otherwise
            # serve a page that posts orders into this session.
            origin = request.headers.get("origin")
            if origin is not None:
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if origin != expected:
                    return Response("cross-origin request refused",
                                    status_code=403)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, token: str = Form(""),
              remember: str = Form("")) -> Response:
        expected = request.app.state.holder.settings().web_token
        if not expected or not hmac.compare_digest(token, expected):
            return templates.TemplateResponse(
                request, "login.html", {"error": "That token is not valid."},
                status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, expected, httponly=True, samesite="strict",
                            max_age=REMEMBER_SECONDS if remember else None)
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response
```

Call `install_auth(app)` in `create_app` right after mounting static files.

- [ ] **Step 5: Write the login template**

Create `allpath_trade/web/templates/login.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sign in · AllPath Trade</title>
  <link rel="stylesheet" href="/static/app.css">
</head>
<body class="centered">
  <form class="card login" method="post" action="/login">
    <h1>allpath trade</h1>
    <p class="muted">Enter your access token to continue</p>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <input type="password" name="token" autofocus autocomplete="current-password">
    <label class="check"><input type="checkbox" name="remember" value="1"> Remember me on this device</label>
    <button type="submit" class="primary">Sign in</button>
    <p class="hint">The token is printed by <code>allpath-trade serve</code> and stored as <code>WEB_TOKEN</code> in <code>.env</code>.</p>
  </form>
</body>
</html>
```

- [ ] **Step 6: Print the token on startup**

In `cmd_serve`, before `uvicorn.run`:

```python
    from allpath_trade.web.auth import ensure_token

    token = ensure_token(SettingsStore(), settings)
    print(f"[allpath-trade] access token: {token}")
```

- [ ] **Step 7: Run, lint, commit**

Run: `uv run pytest tests/test_web_auth.py -v` — the cross-origin test needs `/reviews/1/reject` to exist; until Task 7 lands, assert on `/logout` instead and switch it to the reviews route in Task 7.

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: token auth with same-origin enforcement for the web UI"
```

---

### Task 6: Base layout, stylesheet, and dashboard

**Files:**
- Create: `allpath_trade/web/templates/base.html`, `templates/dashboard.html`, `static/app.css`, `static/htmx.min.js`, `allpath_trade/web/routes/__init__.py`, `allpath_trade/web/routes/dashboard.py`
- Modify: `allpath_trade/web/app.py`
- Test: `tests/test_web_dashboard.py` (create)

**Interfaces:**
- Consumes: `components(request) -> Components`, `templates`
- Produces: `router` (an `APIRouter`) exported from each `routes/*.py`; `nav_context(components) -> dict` with `pending_count`

- [ ] **Step 1: Vendor htmx**

Download htmx 2.x into `allpath_trade/web/static/htmx.min.js`:

```bash
curl -fsSL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js -o allpath_trade/web/static/htmx.min.js
```

Verify it is non-empty and starts with a license comment. The file is committed — the app must run offline.

- [ ] **Step 2: Write the failing dashboard test**

Create `tests/test_web_dashboard.py` (reuse the `client` fixture shape from `tests/test_web_auth.py`, then sign in):

```python
import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker

STRAT = """
name: "Semis core"
status: active
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "semis.yaml").write_text(STRAT)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def test_dashboard_shows_account_and_strategies(client):
    body = client.get("/").text
    assert "Dashboard" in body
    assert "Semis core" in body


def test_dashboard_is_english_only(client):
    body = client.get("/").text
    assert not any("一" <= ch <= "鿿" for ch in body)


def test_broker_outage_does_not_break_the_page(client, monkeypatch):
    holder = client.app.state.holder

    def boom():
        raise RuntimeError("broker down")

    monkeypatch.setattr(holder.get().broker, "get_account", boom)
    r = client.get("/")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_web_dashboard.py -v`
Expected: FAIL — `/` is not routed.

- [ ] **Step 4: Write the stylesheet**

Create `allpath_trade/web/static/app.css`. Dark-first with a light-mode override, one accent, monospace for every number:

```css
:root {
  --bg: #14151a; --surface: #1b1d24; --line: #2b2e38;
  --text: #e6e7ea; --muted: #9a9daa; --accent: #7f9cf5;
  --up: #4ade80; --down: #f87171; --warn: #fbbf24;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#faf9f7; --surface:#fff; --line:#e5e3de; --text:#1a1a1a;
          --muted:#6b6b6b; --accent:#3457b2; --up:#137a3d; --down:#b3261e; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-size:15px;
  line-height:1.6; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
body.centered { display:flex; min-height:100vh; align-items:center; justify-content:center; }
a { color:inherit; text-decoration:none; }
nav { display:flex; gap:18px; align-items:center; padding:14px 20px;
  border-bottom:1px solid var(--line); flex-wrap:wrap; }
nav .brand { font-weight:500; letter-spacing:.02em; margin-right:auto; }
nav a { color:var(--muted); font-size:14px; }
nav a.on { color:var(--text); }
.badge { background:var(--warn); color:#3a2a00; border-radius:999px;
  padding:1px 7px; font-size:12px; margin-left:5px; }
main { max-width:900px; margin:0 auto; padding:20px; }
h1 { font-size:20px; font-weight:500; margin:0 0 16px; }
h2 { font-size:13px; font-weight:400; color:var(--muted); margin:26px 0 10px;
  text-transform:uppercase; letter-spacing:.06em; }
.card { background:var(--surface); border:1px solid var(--line);
  border-radius:12px; padding:16px; }
.metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.metric .label { color:var(--muted); font-size:13px; }
.metric .value { font-family:var(--mono); font-size:24px; }
table { width:100%; border-collapse:collapse; }
td, th { padding:9px 0; border-top:1px solid var(--line); text-align:left;
  font-weight:400; font-size:14px; }
th { color:var(--muted); font-size:12px; border-top:none; }
td.num, th.num { text-align:right; font-family:var(--mono); }
.up { color:var(--up); } .down { color:var(--down); } .muted { color:var(--muted); }
button, .btn { background:transparent; color:var(--text); border:1px solid var(--line);
  border-radius:8px; padding:7px 14px; font-size:14px; cursor:pointer; font:inherit; }
button.primary { border-color:var(--accent); color:var(--accent); }
button.danger { border-color:var(--down); color:var(--down); }
input, select, textarea { background:var(--bg); color:var(--text);
  border:1px solid var(--line); border-radius:8px; padding:8px 10px;
  font:inherit; font-size:14px; width:100%; }
.login { width:320px; }
.login h1 { margin-bottom:2px; }
.hint, .error { font-size:12px; }
.hint { color:var(--muted); margin-top:14px; }
.error { color:var(--down); }
.check { display:flex; gap:8px; align-items:center; font-size:13px;
  color:var(--muted); margin:10px 0 16px; }
.check input { width:auto; }
.row { display:flex; gap:10px; align-items:center; }
.msg { margin-bottom:14px; }
.msg.user { display:flex; justify-content:flex-end; }
.msg.user span { background:var(--surface); border-radius:12px 12px 2px 12px;
  padding:9px 13px; max-width:78%; }
.activity { color:var(--muted); font-size:12px; font-family:var(--mono); }
pre { font-family:var(--mono); font-size:13px; overflow-x:auto;
  background:var(--surface); border:1px solid var(--line);
  border-radius:8px; padding:12px; }
```

- [ ] **Step 5: Write the base template**

Create `allpath_trade/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}AllPath Trade{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/htmx.min.js" defer></script>
</head>
<body>
  <nav>
    <span class="brand">allpath trade</span>
    <a href="/" class="{{ 'on' if page == 'dashboard' }}">Dashboard</a>
    <a href="/chat" class="{{ 'on' if page == 'chat' }}">Chat</a>
    <a href="/reviews" class="{{ 'on' if page == 'reviews' }}">Pending{% if pending_count %}<span class="badge">{{ pending_count }}</span>{% endif %}</a>
    <a href="/strategies" class="{{ 'on' if page == 'strategies' }}">Strategies</a>
    <a href="/memory" class="{{ 'on' if page == 'memory' }}">Memory</a>
    <a href="/settings" class="{{ 'on' if page == 'settings' }}">Settings</a>
  </nav>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

- [ ] **Step 6: Write the dashboard route**

Create `allpath_trade/web/routes/__init__.py` (empty) and `allpath_trade/web/routes/dashboard.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from allpath_trade.app import Components
from allpath_trade.web.templating import templates

router = APIRouter()


def nav_context(components: Components) -> dict:
    return {"pending_count": len(components.queue.list())}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    account = None
    positions: list = []
    broker_error = ""
    try:
        account = c.broker.get_account()
        positions = c.broker.get_positions()
    except Exception as exc:  # noqa: BLE001 — a broker outage must not blank the page
        broker_error = f"Broker unavailable: {exc}"

    errors: list[str] = []
    strategies = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard", "account": account, "positions": positions,
        "broker_error": broker_error, "strategies": strategies,
        "strategy_errors": errors, "trades": c.journal.recent(limit=8),
        **nav_context(c)})
```

- [ ] **Step 7: Write the dashboard template**

Create `allpath_trade/web/templates/dashboard.html`:

```html
{% extends "base.html" %}
{% block title %}Dashboard · AllPath Trade{% endblock %}
{% block content %}
<h1>Dashboard</h1>

{% if broker_error %}<p class="error">{{ broker_error }}</p>{% endif %}
{% if account %}
<div class="metrics">
  <div class="card metric"><div class="label">Equity</div><div class="value">{{ account.equity | money }}</div></div>
  <div class="card metric"><div class="label">Cash</div><div class="value">{{ account.cash | money }}</div></div>
  <div class="card metric"><div class="label">Buying power</div><div class="value">{{ account.buying_power | money }}</div></div>
</div>
{% endif %}

<h2>Positions</h2>
{% if positions %}
<table>
  <tr><th>Ticker</th><th class="num">Qty</th><th class="num">Avg cost</th><th class="num">Value</th><th class="num">P/L</th></tr>
  {% for p in positions %}
  <tr>
    <td>{{ p.ticker }}</td>
    <td class="num">{{ p.qty }}</td>
    <td class="num">{{ p.avg_entry_price | money }}</td>
    <td class="num">{{ p.market_value | money }}</td>
    <td class="num {{ 'up' if p.unrealized_pl and p.unrealized_pl > 0 else 'down' }}">{{ p.unrealized_pl | money }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}<p class="muted">No open positions.</p>{% endif %}

<h2>Active strategies</h2>
{% for s in strategies %}
<div class="card" style="margin-bottom:10px">
  <div class="row">
    <a href="/strategies/{{ s.id }}"><strong>{{ s.name }}</strong></a>
    <span class="muted">· {{ s.position.ticker }}</span>
    <span style="margin-left:auto" class="muted">{{ s.status.value }} / {{ s.authorization.value }}</span>
  </div>
  <div class="muted" style="font-size:13px">
    {% for r in s.rules %}{{ r.id }}: {{ r.state.value }}{% if not loop.last %} · {% endif %}{% endfor %}
  </div>
</div>
{% else %}<p class="muted">No strategies yet. Ask the agent to draft one in Chat.</p>{% endfor %}
{% for e in strategy_errors %}<p class="error">{{ e }}</p>{% endfor %}

<h2>Recent trades</h2>
{% if trades %}
<table>
  <tr><th>When</th><th>Side</th><th>Ticker</th><th>Status</th><th>Reason</th></tr>
  {% for t in trades %}
  <tr><td class="muted">{{ t['ts'] | ago }}</td><td>{{ t['side'] }}</td><td>{{ t['ticker'] }}</td><td>{{ t['status'] }}</td><td class="muted">{{ t['reason'] }}</td></tr>
  {% endfor %}
</table>
{% else %}<p class="muted">No trades recorded yet.</p>{% endif %}
{% endblock %}
```

- [ ] **Step 8: Register the router**

In `create_app`, after `install_auth(app)`:

```python
    from allpath_trade.web.routes import dashboard

    app.include_router(dashboard.router)
```

- [ ] **Step 9: Run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: web dashboard with base layout and stylesheet"
```

---

### Task 7: Pending review queue page

**Files:**
- Create: `allpath_trade/web/routes/reviews.py`, `templates/reviews.html`, `templates/_review_card.html`
- Test: `tests/test_web_reviews.py` (create)
- Modify: `allpath_trade/web/app.py`, `tests/test_web_auth.py` (point the cross-origin test at `/reviews/1/reject`)

**Interfaces:**
- Consumes: `ReviewQueue.list(status)`, `.get(id)`, `.approve(id)`, `.reject(id, note)`
- Produces: routes `GET /reviews`, `POST /reviews/{id}/approve`, `POST /reviews/{id}/reject`; template partial `_review_card.html` rendering one row (reused by the chat page in Task 9)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_reviews.py`:

```python
import json

import pytest
from fastapi.testclient import TestClient

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def queue_one(client, **over) -> int:
    q = client.app.state.holder.get().queue
    kwargs = {"strategy_id": "s1", "rule_id": "r1", "ticker": "AAPL",
              "rule_type": "soft", "condition": "price < 100",
              "action": "sell all", "snapshot": {"price": "99"},
              "intent": OrderIntent(ticker="AAPL", side=OrderSide.SELL,
                                    qty="1", reason="rule r1")}
    kwargs.update(over)
    return q.add(**kwargs)


def test_pending_items_are_listed(client):
    queue_one(client)
    body = client.get("/reviews").text
    assert "AAPL" in body and "Approve" in body


def test_agent_analysis_is_shown(client):
    rid = queue_one(client)
    client.app.state.holder.get().queue.attach_analysis(
        rid, json.dumps({"recommend": "execute", "reasoning": "guidance raised",
                         "sources": ["https://example.com/pr"]}))
    body = client.get("/reviews").text
    assert "guidance raised" in body


def test_approve_executes_through_the_queue(client):
    rid = queue_one(client)
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)
    assert r.status_code in (200, 303)
    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "approved"


def test_reject_records_the_decision(client):
    rid = queue_one(client)
    client.post(f"/reviews/{rid}/reject", follow_redirects=False)
    assert client.app.state.holder.get().queue.get(rid)["status"] == "rejected"


def test_approving_twice_reports_an_error_rather_than_executing_again(client):
    rid = queue_one(client)
    client.post(f"/reviews/{rid}/approve")
    r = client.post(f"/reviews/{rid}/approve")
    assert r.status_code == 200
    assert "not pending" in r.text.lower()


def test_approval_goes_through_the_queue_not_the_executor(client, monkeypatch):
    # Only ReviewQueue.approve writes execution_result and flips the status.
    # If the route ever reached the executor directly, the row would stay
    # pending with an empty execution_result while an order went out.
    rid = queue_one(client)
    client.post(f"/reviews/{rid}/approve")
    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "approved"
    assert row["execution_result"]


def test_a_failing_queue_approve_means_nothing_is_executed(client, monkeypatch):
    executed: list = []
    monkeypatch.setattr(client.app.state.holder.get().executor, "execute",
                        lambda intent: executed.append(intent))
    rid = queue_one(client)
    client.app.state.holder.get().queue.reject(rid, "already handled")
    r = client.post(f"/reviews/{rid}/approve")
    assert "not pending" in r.text.lower()
    assert executed == []
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_web_reviews.py -v`
Expected: FAIL — no `/reviews` route.

- [ ] **Step 3: Write the route**

Create `allpath_trade/web/routes/reviews.py`:

```python
from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.execution import ExecutionError
from allpath_trade.store.reviews import ReviewError
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()


def _decorate(row) -> dict:  # noqa: ANN001 — sqlite3.Row
    item = dict(row)
    for field in ("snapshot", "intent", "agent_analysis", "execution_result"):
        raw = item.get(field)
        item[field] = json.loads(raw) if raw else None
    return item


@router.get("/reviews", response_class=HTMLResponse)
def reviews(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    items = [_decorate(r) for r in c.queue.list("pending")]
    recent = [_decorate(r) for r in c.queue.list(None)][:20]
    return templates.TemplateResponse(request, "reviews.html", {
        "page": "reviews", "items": items,
        "recent": [r for r in recent if r["status"] != "pending"],
        **nav_context(c)})


@router.post("/reviews/{review_id}/approve")
def approve(request: Request, review_id: int) -> Response:
    c = request.app.state.holder.get()
    try:
        result = c.queue.approve(review_id)
    except (ReviewError, ExecutionError) as exc:
        return HTMLResponse(f'<p class="error">{exc}</p>', status_code=200)
    if not result.submitted:
        reasons = "; ".join(result.decision.reasons)
        return HTMLResponse(
            f'<p class="error">Rejected by the risk gate: {reasons}</p>')
    return RedirectResponse("/reviews", status_code=303)


@router.post("/reviews/{review_id}/reject")
def reject(request: Request, review_id: int, note: str = Form("")) -> Response:
    c = request.app.state.holder.get()
    try:
        c.queue.reject(review_id, note)
    except ReviewError as exc:
        return HTMLResponse(f'<p class="error">{exc}</p>', status_code=200)
    return RedirectResponse("/reviews", status_code=303)
```

- [ ] **Step 4: Write the templates**

Create `allpath_trade/web/templates/_review_card.html`:

```html
<div class="card" id="review-{{ item['id'] }}" style="margin-bottom:12px">
  <div class="row">
    <strong>{{ item['action'] }} · {{ item['ticker'] }}</strong>
    <span class="muted">#{{ item['id'] }}</span>
    <span style="margin-left:auto" class="muted">{{ item['ts'] | ago }}</span>
  </div>
  <p class="muted" style="font-size:13px;margin:6px 0">
    {{ item['strategy_id'] }}/{{ item['rule_id'] }} — triggered on <code>{{ item['condition'] }}</code>
  </p>
  {% if item['intent'] %}
  <p style="font-family:var(--mono);font-size:14px;margin:6px 0">
    {{ item['intent']['side'] }}
    {% if item['intent']['qty'] %}{{ item['intent']['qty'] }} shares{% else %}{{ item['intent']['notional'] | money }}{% endif %}
    {{ item['intent']['ticker'] }}
  </p>
  {% endif %}
  {% if item['agent_analysis'] %}
  <div style="border-top:1px solid var(--line);padding-top:10px;margin-top:10px">
    <p style="font-size:13px;margin:0 0 4px"><strong>Agent recommends:</strong> {{ item['agent_analysis'].get('recommend', 'no recommendation') }}</p>
    <p class="muted" style="font-size:13px;margin:0">{{ item['agent_analysis'].get('reasoning', '') }}</p>
    {% for src in item['agent_analysis'].get('sources', []) %}
    <p class="muted" style="font-size:12px;margin:2px 0"><a href="{{ src }}">{{ src }}</a></p>
    {% endfor %}
  </div>
  {% endif %}
  {% if item['status'] == 'pending' %}
  <div class="row" style="margin-top:12px">
    <form method="post" action="/reviews/{{ item['id'] }}/approve"><button class="primary" type="submit">Approve</button></form>
    <form method="post" action="/reviews/{{ item['id'] }}/reject"><button class="danger" type="submit">Reject</button></form>
  </div>
  {% else %}
  <p class="muted" style="font-size:13px;margin:10px 0 0">{{ item['status'] }} {{ item['resolved_ts'] | ago }}</p>
  {% endif %}
</div>
```

Create `allpath_trade/web/templates/reviews.html`:

```html
{% extends "base.html" %}
{% block title %}Pending · AllPath Trade{% endblock %}
{% block content %}
<h1>Pending</h1>
{% for item in items %}{% include "_review_card.html" %}{% else %}
<p class="muted">Nothing waiting on you.</p>
{% endfor %}

{% if recent %}
<h2>Resolved</h2>
{% for item in recent %}{% include "_review_card.html" %}{% endfor %}
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Register and re-point the auth test**

Add `from allpath_trade.web.routes import dashboard, reviews` and `app.include_router(reviews.router)` in `create_app`. In `tests/test_web_auth.py`, restore the cross-origin assertion to target `/reviews/1/reject`.

- [ ] **Step 6: Run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: pending review queue page with approve and reject"
```

---

### Task 8: Queue-backed confirmation for agent-proposed orders

In the terminal, `confirm()` blocks on `input()`. On the web the agent runs server-side, so a proposal becomes a queue entry the user resolves with a button — the turn ends immediately, and a closed tab or a restarted process loses nothing.

**Files:**
- Modify: `allpath_trade/store/db.py` (migrations), `allpath_trade/store/reviews.py`, `allpath_trade/agent/action_tools.py`
- Create: `allpath_trade/web/order_sink.py`
- Test: `tests/test_order_sink.py` (create), `tests/test_action_tools.py` (extend)

**Interfaces:**
- Consumes: `RiskGate.check(intent, account=, positions=, trades_today=, is_paper=, price=) -> RiskDecision`, `ReviewQueue.add(...)`
- Produces:
  - `ReviewQueue.add(..., source: str = "sentinel", conversation_id: int | None = None) -> int`
  - `QueueingOrderSink(queue, gate, broker, data, journal, conversation_id).propose(intent) -> str`
  - `register_action_tools(..., order_sink=None)` — when `order_sink` is given, `propose_order` routes through it instead of `confirm` + `executor.execute`

- [ ] **Step 1: Add the columns**

Extend `_MIGRATIONS` in `allpath_trade/store/db.py`:

```python
    "ALTER TABLE pending_reviews ADD COLUMN source TEXT NOT NULL DEFAULT 'sentinel'",
    "ALTER TABLE pending_reviews ADD COLUMN conversation_id INTEGER",
    "ALTER TABLE pending_reviews ADD COLUMN risk_preview TEXT",
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_order_sink.py`:

```python
from decimal import Decimal

import pytest

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.web.order_sink import QueueingOrderSink
from tests.test_sentinel import FakeBroker


class FakeData:
    def get_price(self, ticker: str) -> Decimal:  # noqa: ARG002
        return Decimal("100")


@pytest.fixture
def sink(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None)
    broker = FakeBroker()
    return QueueingOrderSink(queue, RiskGate(RiskLimits()), broker, FakeData(),
                             TradeJournal(conn), conversation_id=7), queue


def test_proposal_is_queued_not_executed(sink):
    s, queue = sink
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="1000",
                         reason="rebalance")
    message = s.propose(intent)
    rows = queue.list("pending")
    assert len(rows) == 1
    assert rows[0]["source"] == "chat"
    assert rows[0]["conversation_id"] == 7
    assert "#" in message


def test_risk_preview_is_recorded(sink):
    s, queue = sink
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="999999",
                         reason="too big")
    s.propose(intent)
    row = queue.list("pending")[0]
    assert "max_order_value" in row["risk_preview"]


def test_preview_failure_still_queues_the_proposal(sink, monkeypatch):
    s, queue = sink

    def boom(*a, **k):  # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("no quote")

    monkeypatch.setattr(s.data, "get_price", boom)
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="100",
                         reason="x")
    s.propose(intent)
    assert len(queue.list("pending")) == 1
```

- [ ] **Step 3: Run and watch them fail**

Run: `uv run pytest tests/test_order_sink.py -v`
Expected: FAIL — no module `allpath_trade.web.order_sink`.

- [ ] **Step 4: Extend `ReviewQueue.add`**

In `allpath_trade/store/reviews.py`, change the signature and the insert:

```python
    def add(self, *, strategy_id: str, rule_id: str, ticker: str, rule_type: str,
            condition: str, action: str, snapshot: dict,
            intent: OrderIntent | None, source: str = "sentinel",
            conversation_id: int | None = None,
            risk_preview: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent, source,"
            " conversation_id, risk_preview)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), strategy_id, rule_id, ticker,
             rule_type, condition, action,
             json.dumps(snapshot, default=_json_default),
             intent.model_dump_json() if intent else None, source,
             conversation_id, risk_preview))
        self._conn.commit()
        return cur.lastrowid
```

- [ ] **Step 5: Write the sink**

Create `allpath_trade/web/order_sink.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from allpath_trade.broker.base import Broker, OrderIntent
from allpath_trade.data.base import DataSource
from allpath_trade.risk.gate import RiskGate
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue


class QueueingOrderSink:
    """Turns an agent's order proposal into a pending review.

    The web chat cannot block on a y/N prompt, and it must never reach the
    executor itself. Queueing keeps one approval path — the same one the
    sentinel uses — so a proposal survives a closed tab or a restart, and the
    risk gate stays the single chokepoint before the broker."""

    def __init__(self, queue: ReviewQueue, gate: RiskGate, broker: Broker,
                 data: DataSource, journal: TradeJournal,
                 conversation_id: int | None = None) -> None:
        self.queue = queue
        self.gate = gate
        self.broker = broker
        self.data = data
        self.journal = journal
        self.conversation_id = conversation_id

    def propose(self, intent: OrderIntent) -> str:
        preview = self._preview(intent)
        review_id = self.queue.add(
            strategy_id="", rule_id="", ticker=intent.ticker,
            rule_type="chat", condition="proposed in conversation",
            action=intent.reason,
            snapshot={"proposed_ts": datetime.now(UTC).isoformat()},
            intent=intent, source="chat",
            conversation_id=self.conversation_id, risk_preview=preview)
        return (f"queued for the user's approval (#{review_id}). "
                f"Risk pre-check: {preview}")

    def _preview(self, intent: OrderIntent) -> str:
        """Dry-run the risk gate so the card can say whether this would pass.

        Advisory only — the real gate runs again inside Executor at approval
        time, against fresh account state."""
        try:
            price = self.data.get_price(intent.ticker)
            decision = self.gate.check(
                intent, account=self.broker.get_account(),
                positions=self.broker.get_positions(),
                trades_today=self.journal.count_today(),
                is_paper=self.broker.is_paper, price=price)
        except Exception as exc:  # noqa: BLE001 — a failed preview must not block the proposal
            return f"could not be checked ({exc})"
        if decision.approved:
            return "passes"
        return "would be rejected: " + "; ".join(decision.reasons)
```

If `TradeJournal` has no `count_today`, use the same call the executor uses — check `allpath_trade/execution.py` and mirror it exactly.

- [ ] **Step 6: Route `propose_order` through the sink**

In `allpath_trade/agent/action_tools.py`, add `order_sink=None` to `register_action_tools` and branch at the top of `propose_order`, after the `OrderIntent` is built:

```python
        if order_sink is not None:
            return order_sink.propose(intent)
```

Leave the existing confirm-and-execute path untouched below it — the terminal chat still uses it.

- [ ] **Step 7: Add the action-tools test**

Append to `tests/test_action_tools.py`:

```python
def test_order_sink_takes_precedence_over_confirm(tmp_path):
    from allpath_trade.agent.tools import ToolRegistry

    calls: list = []

    class Sink:
        def propose(self, intent):  # noqa: ANN001, ANN201
            calls.append(intent)
            return "queued for the user's approval (#3)"

    registry = ToolRegistry()
    register_action_tools(registry, strategies=None, executor=None,
                          confirm=lambda _: pytest.fail("must not prompt"),
                          order_sink=Sink())
    out = registry.execute(ToolCall(id="1", name="propose_order", arguments={
        "ticker": "AAPL", "side": "buy", "notional": "500", "reason": "test"}))
    assert "#3" in out
    assert calls[0].ticker == "AAPL"
```

- [ ] **Step 8: Run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: queue agent order proposals instead of blocking on confirm"
```

---

### Task 9: Chat page with streamed activity

**Files:**
- Create: `allpath_trade/web/routes/chat.py`, `allpath_trade/web/chat_service.py`, `templates/chat.html`, `templates/_chat_messages.html`
- Test: `tests/test_web_chat.py` (create)

**Interfaces:**
- Consumes: `AgentSession`, `Compactor`, `QueueingOrderSink`, `build_system_prompt`, `ConversationStore`
- Produces:
  - `ChatService(holder).session() -> AgentSession` — one process-wide session, rebuilt when its snapshot is older than 30 minutes
  - `ChatService.send(text) -> str`
  - `ChatService.activity` — a list of activity lines for the current turn
  - Routes `GET /chat`, `POST /chat/send`, `GET /chat/events` (SSE)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_chat.py`:

```python
import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.llm.base import LLMResponse
from allpath_trade.web.app import create_app
from tests.test_agent_loop import ScriptedLLM, tool_response
from tests.test_sentinel import FakeBroker


def make_client(tmp_path, monkeypatch, responses):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        openrouter_api_key="k")
    llm = ScriptedLLM(responses)
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat": llm)
    client = TestClient(create_app(settings, broker=FakeBroker()))
    client.post("/login", data={"token": "secret"})
    return client


def test_message_round_trip(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="hello there")])
    r = client.post("/chat/send", data={"message": "hi"})
    assert "hello there" in r.text


def test_history_survives_a_reload(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="remembered")])
    client.post("/chat/send", data={"message": "hi"})
    assert "remembered" in client.get("/chat").text


def test_no_session_controls_are_offered(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [])
    body = client.get("/chat").text.lower()
    assert "new conversation" not in body
    assert "sessions" not in body


def test_proposed_order_becomes_a_pending_review(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [
        tool_response("propose_order", {"ticker": "AAPL", "side": "buy",
                                        "notional": "500", "reason": "add"}),
        LLMResponse(text="queued it for you"),
    ])
    client.post("/chat/send", data={"message": "buy some apple"})
    rows = client.app.state.holder.get().queue.list("pending")
    assert len(rows) == 1
    assert rows[0]["source"] == "chat"


def test_empty_message_is_ignored(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [])
    r = client.post("/chat/send", data={"message": "   "})
    assert r.status_code == 200
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_web_chat.py -v`
Expected: FAIL — no `/chat` route.

- [ ] **Step 3: Write the chat service**

Create `allpath_trade/web/chat_service.py`:

```python
from __future__ import annotations

import threading
from datetime import UTC, datetime

from allpath_trade.agent.compact import Compactor
from allpath_trade.agent.context import build_system_prompt, load_identity
from allpath_trade.agent.loop import AgentSession
from allpath_trade.agent.memory_tools import register_memory_tools
from allpath_trade.agent.readonly_tools import register_readonly_tools
from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.web.order_sink import QueueingOrderSink

SNAPSHOT_TTL_SECONDS = 30 * 60


class ChatService:
    """One conversation, forever.

    The user never picks or creates a session — the process resumes the latest
    conversation and the Compactor keeps it inside the context budget. The
    system-prompt snapshot is frozen for cache stability, so it is rebuilt once
    it is older than SNAPSHOT_TTL_SECONDS; a long-lived server would otherwise
    reason about yesterday's positions."""

    def __init__(self, holder) -> None:  # noqa: ANN001 — ComponentHolder
        self.holder = holder
        self._lock = threading.Lock()
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
        register_memory_tools(registry, memory=c.memory, conn=c.conn)
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
        self.activity = []
        return self.session().run_turn(text)

    def messages(self) -> list[dict]:
        return list(self.session().history)

    def note_resolution(self, line: str) -> None:
        """Record an out-of-band event (an approval, a fill) in the transcript
        so the agent sees it on its next turn."""
        session = self.session()
        session._append({"role": "user", "content": f"[system] {line}"})
```

`confirm` is wired to always decline: with `order_sink` set, `propose_order` returns before it is consulted, and `draft_strategy` on the web goes through its own confirmation card in a later phase. Returning `False` means a strategy draft reports "user declined" rather than silently saving.

- [ ] **Step 4: Write the routes**

Create `allpath_trade/web/routes/chat.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from allpath_trade.web.chat_service import ChatService
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()


def _service(request: Request) -> ChatService:
    service = getattr(request.app.state, "chat", None)
    if service is None:
        service = ChatService(request.app.state.holder)
        request.app.state.chat = service
    return service


def _render(request: Request, template: str) -> HTMLResponse:
    c = request.app.state.holder.get()
    service = _service(request)
    pending = {r["id"]: dict(r) for r in c.queue.list("pending")}
    return templates.TemplateResponse(request, template, {
        "page": "chat", "messages": service.messages(),
        "activity": service.activity, "pending": pending, **nav_context(c)})


@router.get("/chat", response_class=HTMLResponse)
def chat(request: Request) -> HTMLResponse:
    return _render(request, "chat.html")


@router.post("/chat/send", response_class=HTMLResponse)
def send(request: Request, message: str = Form("")) -> HTMLResponse:
    text = message.strip()
    if text:
        _service(request).send(text)
    return _render(request, "_chat_messages.html")
```

- [ ] **Step 5: Write the templates**

Create `allpath_trade/web/templates/_chat_messages.html`:

```html
<div id="messages">
{% for m in messages %}
  {% if m['role'] == 'user' %}
    <div class="msg user"><span>{{ m['content'] }}</span></div>
  {% elif m['role'] == 'assistant' and m['content'] %}
    <div class="msg">{{ m['content'] }}</div>
  {% endif %}
{% endfor %}
{% if activity %}<div class="activity">{{ activity | join(' · ') }}</div>{% endif %}
{% for id, item in pending.items() %}
  {% if item['source'] == 'chat' %}
  <div class="card" style="margin:12px 0">
    <div class="row">
      <strong>Waiting for your approval</strong>
      <span style="margin-left:auto" class="muted">#{{ id }}</span>
    </div>
    <p style="font-family:var(--mono);margin:8px 0">{{ item['action'] }} · {{ item['ticker'] }}</p>
    <p class="muted" style="font-size:13px;margin:0">Risk pre-check: {{ item['risk_preview'] or 'not checked' }}</p>
    <div class="row" style="margin-top:12px">
      <form method="post" action="/reviews/{{ id }}/approve"><button class="primary" type="submit">Approve</button></form>
      <form method="post" action="/reviews/{{ id }}/reject"><button class="danger" type="submit">Reject</button></form>
    </div>
  </div>
  {% endif %}
{% endfor %}
</div>
```

Create `allpath_trade/web/templates/chat.html`:

```html
{% extends "base.html" %}
{% block title %}Chat · AllPath Trade{% endblock %}
{% block content %}
{% include "_chat_messages.html" %}
<form hx-post="/chat/send" hx-target="#messages" hx-swap="outerHTML"
      hx-on::after-request="this.reset()" class="row" style="margin-top:18px">
  <input name="message" placeholder="Ask your agent anything" autocomplete="off" autofocus>
  <button class="primary" type="submit">Send</button>
</form>
<p class="hint">Orders and strategy changes always wait for your approval.</p>
{% endblock %}
```

- [ ] **Step 6: Register the router and run**

Add `chat` to the imports and `app.include_router(chat.router)` in `create_app`.

Run: `uv run pytest tests/test_web_chat.py -v`
Expected: PASS

- [ ] **Step 7: Feed approvals back into the conversation**

In `allpath_trade/web/routes/reviews.py`, after a successful approve or reject, append a system line when the item came from chat:

```python
    service = getattr(request.app.state, "chat", None)
    if service is not None and row_source == "chat":
        service.note_resolution(
            f"You approved #{review_id}. Result: {summary}")
```

Capture `row_source` and `summary` from the queue row and the `ExecutionResult` before returning. Add the test:

```python
def test_approval_is_echoed_into_the_conversation(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [
        tool_response("propose_order", {"ticker": "AAPL", "side": "buy",
                                        "notional": "500", "reason": "add"}),
        LLMResponse(text="queued"),
    ])
    client.post("/chat/send", data={"message": "buy apple"})
    rid = client.app.state.holder.get().queue.list("pending")[0]["id"]
    client.post(f"/reviews/{rid}/approve")
    history = client.app.state.chat.messages()
    assert any("[system]" in str(m.get("content", "")) for m in history)
```

- [ ] **Step 8: Run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: web chat with inline approval cards"
```

---

### Task 10: Strategies pages (read-only)

**Files:**
- Create: `allpath_trade/web/routes/strategies.py`, `templates/strategies.html`, `templates/strategy_detail.html`
- Test: `tests/test_web_strategies.py` (create)

**Interfaces:**
- Consumes: `StrategyStore.load_all(status, errors)`, `.directory`, `.versions(strategy_id)`, `.set_rule_state(strategy_id, rule_id, state)` — check the exact names in `allpath_trade/strategy/store.py` and use them verbatim
- Produces: routes `GET /strategies`, `GET /strategies/{id}`, `POST /strategies/{id}/rules/{rule_id}/rearm`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_strategies.py` using the `client` fixture from `tests/test_web_dashboard.py` (copy it — tests are read in isolation):

```python
def test_list_shows_each_strategy(client):
    assert "Semis core" in client.get("/strategies").text


def test_detail_shows_yaml_and_rules(client):
    body = client.get("/strategies/semis").text
    assert "target_weight" in body
    assert "r1" in body


def test_unknown_strategy_returns_404(client):
    assert client.get("/strategies/nope").status_code == 404


def test_path_traversal_is_refused(client):
    assert client.get("/strategies/..%2f..%2fetc%2fpasswd").status_code in (400, 404)


def test_rearm_resets_a_triggered_rule(client):
    store = client.app.state.holder.get().strategies
    store.set_rule_state("semis", "r1", "triggered")
    client.post("/strategies/semis/rules/r1/rearm", follow_redirects=False)
    doc = [d for d in store.load_all(status=None, errors=[]) if d.id == "semis"][0]
    assert doc.rules[0].state.value == "armed"
```

Match `set_rule_state`'s real signature; if it takes a `RuleState` enum, import and pass it.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_web_strategies.py -v`

- [ ] **Step 3: Write the route**

Create `allpath_trade/web/routes/strategies.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.strategy.loader import is_valid_strategy_id
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()


@router.get("/strategies", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    errors: list[str] = []
    docs = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "strategies.html", {
        "page": "strategies", "docs": docs, "errors": errors, **nav_context(c)})


@router.get("/strategies/{strategy_id}", response_class=HTMLResponse)
def detail(request: Request, strategy_id: str) -> HTMLResponse:
    if not is_valid_strategy_id(strategy_id):
        raise HTTPException(status_code=404, detail="not found")
    c = request.app.state.holder.get()
    docs = [d for d in c.strategies.load_all(status=None, errors=[])
            if d.id == strategy_id]
    if not docs:
        raise HTTPException(status_code=404, detail="not found")
    path = c.strategies.directory / f"{strategy_id}.yaml"
    return templates.TemplateResponse(request, "strategy_detail.html", {
        "page": "strategies", "doc": docs[0],
        "yaml_text": path.read_text() if path.exists() else "",
        "versions": c.strategies.versions(strategy_id), **nav_context(c)})


@router.post("/strategies/{strategy_id}/rules/{rule_id}/rearm")
def rearm(request: Request, strategy_id: str, rule_id: str) -> Response:
    if not is_valid_strategy_id(strategy_id):
        raise HTTPException(status_code=404, detail="not found")
    c = request.app.state.holder.get()
    c.strategies.set_rule_state(strategy_id, rule_id, "armed")
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)
```

`is_valid_strategy_id` is the same guard the agent's `draft_strategy` uses — it is what keeps `../` out of the path.

- [ ] **Step 4: Write the templates**

`allpath_trade/web/templates/strategies.html`:

```html
{% extends "base.html" %}
{% block title %}Strategies · AllPath Trade{% endblock %}
{% block content %}
<h1>Strategies</h1>
{% for d in docs %}
<div class="card" style="margin-bottom:10px">
  <div class="row">
    <a href="/strategies/{{ d.id }}"><strong>{{ d.name }}</strong></a>
    <span class="muted">· {{ d.position.ticker }}</span>
    <span style="margin-left:auto" class="muted">v{{ d.version }} · {{ d.status.value }} / {{ d.authorization.value }}</span>
  </div>
</div>
{% else %}<p class="muted">No strategies yet. Ask the agent to draft one in Chat.</p>{% endfor %}
{% for e in errors %}<p class="error">{{ e }}</p>{% endfor %}
{% endblock %}
```

`allpath_trade/web/templates/strategy_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ doc.name }} · AllPath Trade{% endblock %}
{% block content %}
<h1>{{ doc.name }}</h1>
<p class="muted">{{ doc.id }} · v{{ doc.version }} · {{ doc.status.value }} / {{ doc.authorization.value }}</p>

<h2>Rules</h2>
<table>
  <tr><th>Rule</th><th>Type</th><th>Condition</th><th>Action</th><th>State</th><th></th></tr>
  {% for r in doc.rules %}
  <tr>
    <td>{{ r.id }}</td><td>{{ r.type.value }}</td>
    <td><code>{{ r.condition }}</code></td><td>{{ r.action }}</td>
    <td>{{ r.state.value }}</td>
    <td class="num">
      {% if r.state.value != 'armed' %}
      <form method="post" action="/strategies/{{ doc.id }}/rules/{{ r.id }}/rearm"><button type="submit">Re-arm</button></form>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>

<h2>Definition</h2>
<pre>{{ yaml_text }}</pre>
<p class="hint">To change a strategy, ask the agent in Chat — it drafts the revision and you approve the diff.</p>

<h2>Version history</h2>
<table>
  <tr><th>Version</th><th>When</th><th>Reason</th></tr>
  {% for v in versions %}
  <tr><td>v{{ v['version'] }}</td><td class="muted">{{ v['ts'] | ago }}</td><td class="muted">{{ v['reason'] }}</td></tr>
  {% endfor %}
</table>
{% endblock %}
```

- [ ] **Step 5: Register, run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: read-only strategies pages with rule re-arm"
```

---

### Task 11: Memory page

**Files:**
- Create: `allpath_trade/web/routes/memory.py`, `templates/memory.html`
- Test: `tests/test_web_memory.py` (create)

**Interfaces:**
- Consumes: `MemoryStore.read(layer, key)`, `LAYER_BUDGETS`, the `memory_log` table
- Produces: route `GET /memory`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_memory.py` (copy the `client` fixture):

```python
def test_layers_are_rendered(client):
    c = client.app.state.holder.get()
    c.memory.apply("profile", None, "add", text="prefers dividend payers")
    body = client.get("/memory").text
    assert "dividend payers" in body
    assert "Profile" in body


def test_audit_trail_is_shown(client):
    c = client.app.state.holder.get()
    c.memory.apply("profile", None, "add", text="likes semis")
    assert "add" in client.get("/memory").text


def test_page_has_no_edit_controls(client):
    body = client.get("/memory").text.lower()
    assert "<textarea" not in body
    assert "delete" not in body
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_web_memory.py -v`

- [ ] **Step 3: Write the route**

Create `allpath_trade/web/routes/memory.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from allpath_trade.memory.store import LAYER_BUDGETS
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

LAYER_TITLES = {"profile": "Profile", "strategy": "Strategy notes",
                "stock": "Stock dossiers", "lessons": "Lessons"}


@router.get("/memory", response_class=HTMLResponse)
def memory(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    layers = []
    for layer in LAYER_BUDGETS:
        if layer == "stock":
            root = c.memory.root / "stock"
            keys = sorted(p.stem for p in root.glob("*.md")) if root.exists() else []
            for key in keys:
                layers.append({"title": f"{LAYER_TITLES[layer]} — {key}",
                               "body": c.memory.read(layer, key)})
            continue
        layers.append({"title": LAYER_TITLES.get(layer, layer),
                       "body": c.memory.read(layer)})
    log = list(c.conn.execute(
        "SELECT ts, layer, key, action, after FROM memory_log"
        " ORDER BY id DESC LIMIT 30"))
    return templates.TemplateResponse(request, "memory.html", {
        "page": "memory", "layers": layers, "log": log, **nav_context(c)})
```

Confirm the attribute holding the memory root directory in `allpath_trade/memory/store.py` (it is set in `__init__` from the `root` argument) and use that exact name.

- [ ] **Step 4: Write the template**

`allpath_trade/web/templates/memory.html`:

```html
{% extends "base.html" %}
{% block title %}Memory · AllPath Trade{% endblock %}
{% block content %}
<h1>Memory</h1>
<p class="hint">Memory is written by the agent during consolidation, and every write is scanned for injected instructions. To change something, tell the agent in Chat.</p>

{% for layer in layers %}
<h2>{{ layer.title }}</h2>
{% if layer.body.strip() %}<pre>{{ layer.body }}</pre>
{% else %}<p class="muted">Empty.</p>{% endif %}
{% endfor %}

<h2>Recent changes</h2>
{% if log %}
<table>
  <tr><th>When</th><th>Layer</th><th>Action</th><th>Content</th></tr>
  {% for r in log %}
  <tr>
    <td class="muted">{{ r['ts'] | ago }}</td>
    <td>{{ r['layer'] }}{% if r['key'] %} / {{ r['key'] }}{% endif %}</td>
    <td>{{ r['action'] }}</td>
    <td class="muted">{{ (r['after'] or '')[:120] }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}<p class="muted">Nothing recorded yet.</p>{% endif %}
{% endblock %}
```

- [ ] **Step 5: Register, run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: read-only memory page with change audit"
```

---

### Task 12: Settings page

**Files:**
- Create: `allpath_trade/web/routes/settings.py`, `templates/settings.html`
- Test: `tests/test_web_settings.py` (create)

**Interfaces:**
- Consumes: `SettingsStore.get/set/load`, `ComponentHolder.rebuild()`
- Produces: routes `GET /settings`, `POST /settings`, `POST /settings/test-email`, `POST /settings/reset-token`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_settings.py` (copy the `client` fixture, and point `SettingsStore` at the tmp `.env`):

```python
def test_secrets_are_never_echoed_back(client, tmp_path):
    (tmp_path / ".env").write_text('ALPACA_SECRET_KEY="supersecret"\n')
    client.app.state.holder.rebuild()
    body = client.get("/settings").text
    assert "supersecret" not in body
    assert "Replace" in body


def test_saving_writes_env_and_rebuilds(client, tmp_path):
    r = client.post("/settings", data={
        "chat_model": "anthropic/claude-opus-5",
        "sentinel_interval_minutes": "30",
        "smtp_from": "AllPath Trade <bot@example.com>",
    }, follow_redirects=False)
    assert r.status_code == 303
    text = (tmp_path / ".env").read_text()
    assert "anthropic/claude-opus-5" in text
    assert "AllPath Trade <bot@example.com>" in text


def test_blank_secret_field_leaves_the_stored_value_alone(client, tmp_path):
    (tmp_path / ".env").write_text('OPENROUTER_API_KEY="keep-me"\n')
    client.post("/settings", data={"openrouter_api_key": ""})
    assert "keep-me" in (tmp_path / ".env").read_text()


def test_paper_mode_cannot_be_switched_from_the_page(client):
    body = client.get("/settings").text
    assert 'name="alpaca_paper"' not in body
    client.post("/settings", data={"alpaca_paper": "false"})
    assert client.app.state.holder.get().settings.alpaca_paper is True


def test_reset_token_invalidates_the_session(client):
    client.post("/settings/reset-token", follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_web_settings.py -v`

- [ ] **Step 3: Write the route**

Create `allpath_trade/web/routes/settings.py`:

```python
from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.config import SettingsStore
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

# Plain values: rendered, editable, rewritten on every save.
PLAIN_FIELDS = ["llm_provider", "chat_model", "review_model", "memory_model",
                "smtp_host", "smtp_port", "smtp_user", "smtp_from", "notify_to",
                "sentinel_interval_minutes", "context_budget_tokens",
                "daily_consolidation", "consolidate_after_chat"]

# Secret values: never rendered back. A blank field means "leave it alone".
SECRET_FIELDS = ["openrouter_api_key", "openai_api_key", "anthropic_api_key",
                 "alpaca_api_key", "alpaca_secret_key", "smtp_password"]


def _mask(value: str) -> str:
    if not value:
        return ""
    return f"{value[:6]}{'•' * 8}{value[-4:]}" if len(value) > 12 else "•" * 8


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", note: str = "") -> HTMLResponse:
    c = request.app.state.holder.get()
    s = c.settings
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "s": s, "saved": bool(saved), "note": note,
        "masks": {f: _mask(str(getattr(s, f, ""))) for f in SECRET_FIELDS},
        **nav_context(c)})


@router.post("/settings")
async def save(request: Request) -> Response:
    form = await request.form()
    store = SettingsStore()
    for field in PLAIN_FIELDS:
        if field in form:
            store.set(field.upper(), str(form[field]).strip())
    for field in SECRET_FIELDS:
        value = str(form.get(field, "")).strip()
        if value:  # blank means "keep what is stored"
            store.set(field.upper(), value)
    # ALPACA_PAPER is deliberately absent: switching to real money should
    # require editing .env by hand.
    request.app.state.holder.rebuild()
    request.app.state.chat = None  # next turn picks up the new configuration
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/test-email")
def test_email(request: Request) -> Response:
    c = request.app.state.holder.get()
    c.notifier.send("AllPath Trade test",
                    "This is a test notification. If you are reading it, "
                    "email delivery works.")
    return RedirectResponse("/settings?note=Test+email+sent", status_code=303)


@router.post("/settings/reset-token")
def reset_token(request: Request) -> Response:
    token = secrets.token_urlsafe(24)
    SettingsStore().set("WEB_TOKEN", token)
    request.app.state.holder.rebuild()
    print(f"[allpath-trade] new access token: {token}")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("allpath_session")
    return response
```

- [ ] **Step 4: Write the template**

`allpath_trade/web/templates/settings.html` — one form, five sections. Secret rows render the mask and an empty input labelled "Replace":

```html
{% extends "base.html" %}
{% block title %}Settings · AllPath Trade{% endblock %}
{% block content %}
<h1>Settings</h1>
{% if saved %}<p class="muted">Saved. New conversations use the updated configuration.</p>{% endif %}
{% if note %}<p class="muted">{{ note }}</p>{% endif %}

<form method="post" action="/settings">
  <h2>Model</h2>
  <div class="card">
    <label>Provider</label>
    <select name="llm_provider">
      {% for p in ['openrouter', 'openai', 'anthropic'] %}
      <option value="{{ p }}" {{ 'selected' if s.llm_provider == p }}>{{ p }}</option>
      {% endfor %}
    </select>
    <label>OpenRouter API key <span class="muted">{{ masks['openrouter_api_key'] or 'not set' }}</span></label>
    <input name="openrouter_api_key" type="password" placeholder="Replace" autocomplete="off">
    <label>Chat model</label><input name="chat_model" value="{{ s.chat_model }}">
    <label>Sentinel review model</label><input name="review_model" value="{{ s.review_model }}">
    <label>Memory model</label><input name="memory_model" value="{{ s.memory_model }}">
  </div>

  <h2>Brokerage</h2>
  <div class="card">
    <p class="muted">Alpaca · {{ 'paper' if s.alpaca_paper else 'LIVE' }}</p>
    <label>API key <span class="muted">{{ masks['alpaca_api_key'] or 'not set' }}</span></label>
    <input name="alpaca_api_key" type="password" placeholder="Replace" autocomplete="off">
    <label>Secret key <span class="muted">{{ masks['alpaca_secret_key'] or 'not set' }}</span></label>
    <input name="alpaca_secret_key" type="password" placeholder="Replace" autocomplete="off">
    <p class="hint">Switching to live trading is not available here. Edit <code>ALPACA_PAPER</code> in <code>.env</code> — connecting real money should take a deliberate step.</p>
  </div>

  <h2>Email notifications</h2>
  <div class="card">
    <label>SMTP host</label><input name="smtp_host" value="{{ s.smtp_host }}">
    <label>Port</label><input name="smtp_port" value="{{ s.smtp_port }}">
    <label>Sender account</label><input name="smtp_user" value="{{ s.smtp_user }}">
    <label>From header</label><input name="smtp_from" value="{{ s.smtp_from }}">
    <label>App password <span class="muted">{{ masks['smtp_password'] or 'not set' }}</span></label>
    <input name="smtp_password" type="password" placeholder="Replace" autocomplete="off">
    <label>Send notifications to</label><input name="notify_to" value="{{ s.notify_to }}">
    <p class="hint">Notifications report what happened; they never contain action links.</p>
  </div>

  <h2>Sentinel and memory</h2>
  <div class="card">
    <label>Check interval (minutes)</label><input name="sentinel_interval_minutes" value="{{ s.sentinel_interval_minutes }}">
    <label>Context budget (tokens)</label><input name="context_budget_tokens" value="{{ s.context_budget_tokens }}">
    <label class="check"><input type="checkbox" name="daily_consolidation" value="true" {{ 'checked' if s.daily_consolidation }}> Consolidate memory after the close</label>
    <label class="check"><input type="checkbox" name="consolidate_after_chat" value="true" {{ 'checked' if s.consolidate_after_chat }}> Consolidate after each conversation</label>
  </div>

  <h2>Access</h2>
  <div class="card">
    <p class="muted">Listening on {{ s.web_host }}:{{ s.web_port }}</p>
  </div>

  <div class="row" style="margin-top:18px">
    <button class="primary" type="submit">Save settings</button>
  </div>
</form>

<div class="row" style="margin-top:12px">
  <form method="post" action="/settings/test-email"><button type="submit">Send test email</button></form>
  <form method="post" action="/settings/reset-token"><button class="danger" type="submit">Reset access token</button></form>
</div>
{% endblock %}
```

Unchecked checkboxes are absent from the form body, so `daily_consolidation` and `consolidate_after_chat` need explicit handling in `save`: set them to `"false"` when the key is missing. Add that before the `PLAIN_FIELDS` loop:

```python
    booleans = {"daily_consolidation", "consolidate_after_chat"}
    for field in booleans:
        store.set(field.upper(), "true" if form.get(field) else "false")
```

and remove those two names from `PLAIN_FIELDS`.

- [ ] **Step 5: Register, run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: settings page with write-only secrets and live rebuild"
```

---

### Task 13: Email notifications for real events

**Files:**
- Create: `allpath_trade/notify/events.py`, `tests/test_notify_events.py`
- Modify: `allpath_trade/sentinel.py`, `allpath_trade/store/reviews.py`

**Interfaces:**
- Consumes: `Notifier.send(subject, body)`
- Produces: `rule_triggered(...)`, `order_result(...)`, `review_queued(...)`, `daily_digest(...)` — each returns `(subject, body)` as English text with no links

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notify_events.py`:

```python
from allpath_trade.notify import events


def test_bodies_contain_no_links_or_html():
    subject, body = events.review_queued(
        review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        recommendation="execute")
    assert "http" not in body.lower()
    assert "<" not in body
    assert "12" in body and "AAPL" in subject


def test_bodies_are_english_only():
    for subject, body in [
        events.rule_triggered(strategy_id="s", rule_id="r", ticker="AAPL",
                              condition="price < 100", disposition="queued"),
        events.order_result(ticker="AAPL", side="buy", submitted=True,
                            detail="filled 3 @ 220.15"),
        events.daily_digest(triggers=2, trades=1, pending=3),
    ]:
        for text in (subject, body):
            assert not any("一" <= ch <= "鿿" for ch in text)
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_notify_events.py -v`

- [ ] **Step 3: Write the bodies**

Create `allpath_trade/notify/events.py`:

```python
from __future__ import annotations

FOOTER = ("\n\nOpen the AllPath Trade dashboard to act on this. "
          "This message contains no links by design — an emailed link that "
          "carries your access token would turn a leaked inbox into a leaked "
          "account.")


def rule_triggered(*, strategy_id: str, rule_id: str, ticker: str,
                   condition: str, disposition: str) -> tuple[str, str]:
    subject = f"[AllPath] {ticker}: rule {rule_id} triggered"
    body = (f"Strategy {strategy_id}, rule {rule_id} triggered on {ticker}.\n"
            f"Condition: {condition}\n"
            f"Disposition: {disposition}." + FOOTER)
    return subject, body


def order_result(*, ticker: str, side: str, submitted: bool,
                 detail: str) -> tuple[str, str]:
    outcome = "submitted" if submitted else "not submitted"
    subject = f"[AllPath] {ticker}: order {outcome}"
    body = f"A {side} order for {ticker} was {outcome}.\n{detail}" + FOOTER
    return subject, body


def review_queued(*, review_id: int, ticker: str, action: str,
                  strategy_id: str, recommendation: str = "") -> tuple[str, str]:
    subject = f"[AllPath] {ticker}: waiting for your approval"
    lines = [f"Item #{review_id} is waiting for you.",
             f"Proposed: {action} on {ticker}"]
    if strategy_id:
        lines.append(f"Strategy: {strategy_id}")
    if recommendation:
        lines.append(f"The agent recommends: {recommendation}")
    return subject, "\n".join(lines) + FOOTER


def daily_digest(*, triggers: int, trades: int, pending: int) -> tuple[str, str]:
    subject = "[AllPath] Daily summary"
    body = (f"Today: {triggers} rule trigger(s), {trades} trade(s), "
            f"{pending} item(s) still waiting for your approval." + FOOTER)
    return subject, body
```

- [ ] **Step 4: Emit them from the sentinel**

In `allpath_trade/sentinel.py`, replace each ad-hoc notification string with the corresponding helper. Find the existing `self.notifier.send(...)` calls in `_dispatch` and swap the arguments for `events.rule_triggered(...)` / `events.review_queued(...)` / `events.order_result(...)`, unpacking the tuple:

```python
        subject, body = events.rule_triggered(
            strategy_id=doc.id, rule_id=rule.id, ticker=doc.position.ticker,
            condition=rule.condition, disposition=disposition)
        self.notifier.send(subject, body)
```

Keep the existing behaviour that a notification failure never propagates.

- [ ] **Step 5: Add the digest to the scheduler job**

In `build_jobs` (Task 4), after the consolidation block:

```python
        if _is_after_close() and state["last_daily"] == today:
            subject, body = events.daily_digest(
                triggers=0, trades=len(components.journal.today()),
                pending=len(components.queue.list()))
            components.notifier.send(subject, body)
```

Use whatever "today's trades" accessor `TradeJournal` actually exposes — check the class and use the real method name.

- [ ] **Step 6: Run, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add -A
git commit -m "feat: English notification bodies wired to sentinel events"
```

---

### Task 14: Documentation

**Files:**
- Modify: `README.md`, `README.zh-CN.md`, `docs/TODO.md`, `.env.example`

- [ ] **Step 1: Update the READMEs**

In both READMEs: add `serve` to the Getting Started flow, mark Phase 5 complete in the Roadmap and Phase 6 as next, and add a short "Web interface" section covering the token, LAN access, and the six pages. English content in `README.md`, Chinese in `README.zh-CN.md`.

Add to Getting Started:

````markdown
### Run the web interface

```bash
uv run allpath-trade serve
```

Open `http://localhost:8791`. The access token is printed on startup and
stored as `WEB_TOKEN` in `.env`. To reach it from your phone on the same
network, bind to all interfaces:

```bash
uv run allpath-trade serve --host 0.0.0.0
```

The sentinel runs inside the same process, so this one command covers
monitoring, consolidation, and the interface.
````

- [ ] **Step 2: Close out the TODO entries**

In `docs/TODO.md`, remove the two items resolved in Task 1 (`SettingsStore` quoting, `MemoryError` rename) and the consolidation-toggle item resolved in Task 12. Add anything Phase 5 deliberately deferred:

```markdown
## Phase 5 遗留
- [ ] 策略 YAML 在线编辑（当前只读，修改走聊天让 agent 起草）
- [ ] SSE 实时推送工具活动（当前为回合结束后整体刷新）
- [ ] 手机推送通道（ntfy / Bark），比邮件即时
- [ ] `serve` 的 HTTPS / 反向代理部署文档
```

- [ ] **Step 3: Verify the whole thing runs**

```bash
uv run pytest -q
uv run ruff check .
uv run allpath-trade serve --port 8791
```

Open the printed URL, sign in with the printed token, and click through all six pages. Confirm no page shows Chinese text.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs: Phase 5 web interface in READMEs and TODO"
```

---

## Self-Review Notes

Spec coverage check against §5.4:

| Spec requirement | Task |
|---|---|
| English-only UI | Global constraints; asserted in Tasks 6 and 13 |
| `serve` with in-process scheduler, port 8791 | Task 4 |
| FastAPI + Jinja2 + vendored htmx, no build step | Tasks 4, 6 |
| WAL + safe concurrent access | Task 2 |
| Token auth, Origin check, token reset | Tasks 5, 12 |
| Six pages, strategies/memory read-only | Tasks 6, 7, 9, 10, 11, 12 |
| Queue-based confirmation with risk preview | Task 8 |
| Approval echoed back into the conversation | Task 9 |
| Context compaction, flush before compacting | Task 3 |
| No session management in the UI | Tasks 9 (asserted), 3 |
| Snapshot rebuilt after 30 minutes | Task 9 |
| Email events, no links, test email | Tasks 12, 13 |
| Write-only secrets | Task 12 |
| Live trading not switchable from the UI | Task 12 |
| Three prerequisite fixes | Tasks 1, 2 |

Deviation to note: the spec says "per-thread connections"; Task 2 implements a
serialized shared connection instead. Same guarantee for a single-process app,
and it avoids threading a pool through every store constructor. Update the
spec's §5.4 wording as part of Task 2's commit.

Two interfaces are named in this plan but must be confirmed against the real
code before use — `StrategyStore.versions` / `set_rule_state` (Task 10) and
`TradeJournal`'s today's-trades accessor (Tasks 8, 13). Read the class, use
the real name, and adjust the surrounding code if the signature differs.
