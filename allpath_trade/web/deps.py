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


def holder(request) -> ComponentHolder:  # request: FastAPI Request
    return request.app.state.holder


def components(request) -> Components:
    return request.app.state.holder.get()
