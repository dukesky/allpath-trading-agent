from __future__ import annotations

import contextlib
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

    def store(self) -> SettingsStore:
        # Routes that write `.env` (the settings page) must go through this
        # store, not construct their own `SettingsStore()` -- a route-local
        # store defaults to `.env` relative to the process cwd, which only
        # agrees with the store `rebuild()` reads from because `create_app`
        # never passes a non-default `env_file` today. Sharing this one
        # makes that agreement structural instead of coincidental.
        return self._store

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
            # options-via-mcp T7 review fix: the swapped-out graph's paper
            # bundle may hold a live `McpOptionsBackend` -- a `uvx
            # alpaca-mcp-server` subprocess plus a daemon thread once any
            # option call has happened -- and `build_components` always
            # constructs a FRESH backend for the new graph, never reusing
            # the old one. Its atexit hook only fires at process exit, so
            # without this every settings-page save would leak another
            # subprocess/thread for the life of the process. Same shape as
            # the `stale_conn.close()` above: tear down what the new graph
            # no longer references, after the swap has committed (in-flight
            # work against the old graph may then fail, exactly like work
            # against a closed stale connection). Reached via the executor
            # (the Sentinel shares the same instance, so one `stop()`
            # covers both) with getattr-tolerance because tests drive
            # `rebuild()` with minimal fake Components; `stop()` is
            # idempotent, and one backend's failure to stop must not skip
            # the others or fail the rebuild itself.
            for bundle in getattr(current, "accounts", {}).values():
                backend = getattr(getattr(bundle, "executor", None),
                                  "options_backend", None)
                if backend is not None:
                    with contextlib.suppress(Exception):
                        backend.stop()


def holder(request) -> ComponentHolder:  # request: FastAPI Request
    return request.app.state.holder


def components(request) -> Components:
    return request.app.state.holder.get()
