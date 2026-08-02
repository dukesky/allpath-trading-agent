from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from allpath_trade.memory.guard import scan_entry

LAYER_BUDGETS: dict[str, int] = {
    "profile": 2000, "strategy": 2000, "stock": 3000, "lesson": 2000,
}
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
TRUNCATION_MARKER = "\n…(truncated — use session_search)"


class MemoryStoreError(Exception):
    """Raised for invalid layers, keys, actions, or budget overruns."""


class MemoryStore:
    """Curated memory: human-readable markdown files, entry-level edits only.
    Every mutation is diff-logged to SQLite. The files are the user's to read
    and edit; the agent gets no other write path."""

    def __init__(self, root: Path, conn: sqlite3.Connection) -> None:
        self.root = root
        self._conn = conn

    def path_for(self, layer: str, key: str | None) -> Path:
        if layer == "profile":
            return self.root / "user_profile.md"
        subdir = {"strategy": "strategies", "stock": "stocks",
                  "lesson": "lessons"}.get(layer)
        if subdir is None:
            raise MemoryStoreError(f"unknown memory layer: {layer!r}")
        if not key or not _KEY_RE.match(key):
            raise MemoryStoreError(f"invalid memory key: {key!r}")
        if layer == "stock":
            key = key.upper()
        return self.root / subdir / f"{key}.md"

    def read(self, layer: str, key: str | None = None) -> str:
        path = self.path_for(layer, key)
        return path.read_text() if path.exists() else ""

    def entries(self, layer: str, key: str | None = None) -> list[str]:
        blocks = [b.strip() for b in self.read(layer, key).split("\n\n")]
        return [b for b in blocks if b.startswith("- ")]

    def apply(self, layer: str, key: str | None, action: str,
              text: str | None = None, match: str | None = None) -> str:
        if action in ("add", "replace") and text is not None:
            scan_entry(text)
        path = self.path_for(layer, key)
        before = self.read(layer, key)
        budget = LAYER_BUDGETS[layer]

        if action == "add":
            if text is None:
                raise MemoryStoreError("add requires text")
            if len(before) >= budget:
                raise MemoryStoreError(
                    f"{path.name} is over its {budget}-char budget — "
                    "replace or remove entries instead of adding")
            entry = text if text.startswith("- ") else f"- {text}"
            after = (before.rstrip() + "\n\n" + entry + "\n") if before.strip() \
                else entry + "\n"
        elif action in ("replace", "remove"):
            if not match:
                raise MemoryStoreError(f"{action} requires match")
            blocks = [b for b in before.split("\n\n")]
            hits = [i for i, b in enumerate(blocks)
                    if b.strip().startswith("- ") and match in b]
            if not hits:
                raise MemoryStoreError(f"no entry matches {match!r}")
            if len(hits) > 1:
                raise MemoryStoreError(
                    f"{len(hits)} entries match {match!r} — be more specific")
            if action == "remove":
                del blocks[hits[0]]
            else:
                if text is None:
                    raise MemoryStoreError("replace requires text")
                blocks[hits[0]] = text if text.startswith("- ") else f"- {text}"
            after = "\n\n".join(b for b in blocks if b.strip()) + "\n"
        else:
            raise MemoryStoreError(f"unknown action: {action!r}")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after)
        self._conn.execute(
            "INSERT INTO memory_log (ts, layer, key, action, before, after)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), layer, key, action, before, after))
        self._conn.commit()
        rel = path.relative_to(self.root)
        return f"{action} ok: {rel}"

    def render_for_context(self, layer: str, key: str | None = None,
                           budget: int | None = None) -> str:
        text = self.read(layer, key)
        budget = budget or LAYER_BUDGETS[layer]
        if len(text) <= budget:
            return text
        return text[:budget] + TRUNCATION_MARKER
