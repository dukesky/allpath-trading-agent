from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from allpath_trade.memory.guard import scan_entry
from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account

LAYER_BUDGETS: dict[str, int] = {
    "profile": 2000, "strategy": 2000, "stock": 3000, "lesson": 2000,
}
# Accounts whose profile WRITES go to their own file (`<root>/<account>/
# user_profile.md`) instead of the shared one at the root. See `path_for`.
PRIVATE_PROFILE_ACCOUNTS = ("shadow",)
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
TRUNCATION_MARKER = "\n…(truncated — use session_search)"


class MemoryStoreError(Exception):
    """Raised for invalid layers, keys, actions, or budget overruns."""


class MemoryStore:
    """Curated memory: human-readable markdown files, entry-level edits only.
    Every mutation is diff-logged to SQLite. The files are the user's to read
    and edit; the agent gets no other write path."""

    def __init__(self, root: Path, conn: sqlite3.Connection,
                account: str = DEFAULT_ACCOUNT) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self.root = root
        self._conn = conn
        self.account = account

    def path_for(self, layer: str, key: str | None) -> Path:
        if layer == "profile":
            # The profile at the memory root is the SHARED one: paper reads
            # and writes it, and it is the only profile paper ever sees.
            # An account in PRIVATE_PROFILE_ACCOUNTS (shadow) reads it too
            # but never writes it: its own profile entries go to a file
            # under its account directory, which no other account reads
            # (TDSC red-team F5/M6: what the shadow account learns about the
            # owner's real holdings and trades must not reach paper's
            # prompts through shared memory).
            if self.account in PRIVATE_PROFILE_ACCOUNTS:
                return self.root / self.account / "user_profile.md"
            return self.root / "user_profile.md"
        subdir = {"strategy": "strategies", "stock": "stocks",
                  "lesson": "lessons"}.get(layer)
        if subdir is None:
            raise MemoryStoreError(f"unknown memory layer: {layer!r}")
        if not key or not _KEY_RE.match(key):
            raise MemoryStoreError(f"invalid memory key: {key!r}")
        if layer == "stock":
            key = key.upper()
        return self.root / self.account / subdir / f"{key}.md"

    def _own(self, layer: str, key: str | None = None) -> str:
        """The text of the one file this store WRITES for `layer`/`key`."""
        path = self.path_for(layer, key)
        return path.read_text() if path.exists() else ""

    def shared_profile(self) -> str:
        """The shared profile at the memory root; "" for an account whose
        own profile IS that file (it is then just `read("profile")`)."""
        if self.account not in PRIVATE_PROFILE_ACCOUNTS:
            return ""
        path = self.root / "user_profile.md"
        return path.read_text() if path.exists() else ""

    def read(self, layer: str, key: str | None = None) -> str:
        own = self._own(layer, key)
        if layer != "profile":
            return own
        shared = self.shared_profile()
        if not shared.strip():
            return own
        if not own.strip():
            return shared
        return shared.rstrip() + "\n\n" + own

    def entries(self, layer: str, key: str | None = None) -> list[str]:
        blocks = [b.strip() for b in self.read(layer, key).split("\n\n")]
        return [b for b in blocks if b.startswith("- ")]

    def apply(self, layer: str, key: str | None, action: str,
              text: str | None = None, match: str | None = None) -> str:
        if action in ("add", "replace") and text is not None:
            scan_entry(text)
        path = self.path_for(layer, key)
        before = self._own(layer, key)
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
                if layer == "profile" and match in self.shared_profile():
                    raise MemoryStoreError(
                        f"the entry matching {match!r} is in the shared profile, which "
                        f"the {self.account} account can read but not change; add this "
                        "account's own entry instead")
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
            "INSERT INTO memory_log (account, ts, layer, key, action, before, after)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.account, datetime.now(UTC).isoformat(), layer, key, action,
             before, after))
        self._conn.commit()
        rel = path.relative_to(self.root)
        return f"{action} ok: {rel}"

    def recent_log(self, limit: int = 30) -> list[sqlite3.Row]:
        """The change-audit trail the web memory page renders. Kept behind
        this API like every other store the web layer reads from, rather
        than a route querying `memory_log` with its own raw SQL.

        Scoped to this store's account: the log rows carry the full `after`
        text of a note, so an unscoped query hands one account's Changes
        tab the other account's note bodies verbatim. Note the consequence
        for the SHARED profile layer -- a profile edit made while viewing
        shadow is logged under 'shadow' and so only shows up in shadow's
        Changes tab, even though the file itself is shared. Scoping the
        audit trail to who made the change is the conservative side of
        that trade."""
        return list(self._conn.execute(
            "SELECT ts, layer, key, action, after FROM memory_log"
            " WHERE account = ? ORDER BY id DESC LIMIT ?",
            (self.account, limit)))

    def render_for_context(self, layer: str, key: str | None = None,
                           budget: int | None = None) -> str:
        budget = budget or LAYER_BUDGETS[layer]

        def cut(text: str) -> str:
            return text if len(text) <= budget else text[:budget] + TRUNCATION_MARKER

        if layer == "profile" and self.account in PRIVATE_PROFILE_ACCOUNTS:
            # Each part gets the layer budget: this account's own entries
            # must not be crowded out by a full shared profile.
            shared, own = self.shared_profile(), self._own(layer, key)
            if not own.strip():
                return cut(shared)
            if not shared.strip():
                return cut(own)
            return cut(shared).rstrip() + "\n\n" + cut(own)
        return cut(self.read(layer, key))
