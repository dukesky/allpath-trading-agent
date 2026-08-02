from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from allpath_trade.app import Components, build_components
from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings, SettingsStore

Builder = Callable[[Settings, Broker | None, sqlite3.Connection | None], Components]


class ComponentHolder:
    """Owns the component graph for the life of the process.

    The settings page can rewrite `.env` at any time, so the graph is
    rebuildable: `rebuild()` swaps in a fresh one and new requests pick it up.
    When the new settings keep the same `db_path` (the common case — a
    settings save that doesn't touch the database file), the rebuilt graph
    reuses the existing sqlite connection: conversations already in flight
    keep the objects they captured, and there is nothing to close. When
    `db_path` changes, the rebuilt graph opens a fresh connection and the old
    one is closed once the swap completes — work still in flight against the
    old connection may then fail, since its underlying file handle is gone.
    Overlapping `rebuild()` calls (e.g. two near-simultaneous settings saves)
    are serialized against each other, so at most one rebuild runs at a
    time; `get()` never blocks on that."""

    def __init__(self, settings: Settings, broker: Broker | None = None,
                 builder: Builder | None = None,
                 env_file: Path = Path(".env")) -> None:
        self._broker = broker
        self._builder = builder or build_components
        self._store = SettingsStore(env_file)
        self._lock = threading.Lock()
        # Separate from `_lock`: `_lock` only ever guards a single read or
        # write of `self._components`, so it cannot serialize the multi-step
        # read-build-commit-close sequence below against a second, concurrent
        # `rebuild()` call. `_rebuild_lock` does that instead, while `get()`
        # keeps using only `_lock` so it never blocks on a rebuild in
        # progress.
        self._rebuild_lock = threading.Lock()
        self._components = self._builder(settings, broker, None)

    def get(self) -> Components:
        with self._lock:
            return self._components

    def settings(self) -> Settings:
        return self.get().settings

    def rebuild(self, settings: Settings | None = None) -> None:
        # Hold `_rebuild_lock` for the entire sequence, not just the
        # individual read and write. Two overlapping calls (e.g. a
        # double-submitted settings save, or two browser tabs, both hit sync
        # route handlers running in FastAPI's thread pool) would otherwise
        # both snapshot `current` before either commits: whichever commits
        # last can install a `Components` pointing at the connection the
        # other call already closed, or leave a freshly opened connection
        # referenced by nothing and never closed. Serializing the whole
        # thing makes each `rebuild()` atomic with respect to the others.
        with self._rebuild_lock:
            fresh = settings or self._store.load()
            with self._lock:
                current = self._components
            if fresh.db_path == current.settings.db_path:
                # Same file: reuse the one connection rather than opening a
                # second one in front of it (two LockedConnection locks
                # guarding the same file would not know about each other).
                built = self._builder(fresh, self._broker, current.conn)
                stale_conn = None
            else:
                built = self._builder(fresh, self._broker, None)
                stale_conn = current.conn
            with self._lock:
                self._components = built
            if stale_conn is not None:
                stale_conn.close()


def holder(request) -> ComponentHolder:  # request: FastAPI Request
    return request.app.state.holder


def components(request) -> Components:
    return request.app.state.holder.get()
