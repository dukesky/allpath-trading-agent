from __future__ import annotations

from typing import TYPE_CHECKING

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.memory.guard import MemoryGuardError, scan_entry
from allpath_trade.memory.store import MemoryStore, MemoryStoreError

if TYPE_CHECKING:
    from allpath_trade.memory.search import SessionSearch

_LAYERS = ("profile", "strategy", "stock", "lesson")


def register_memory_tools(registry: ToolRegistry, *, memory: MemoryStore,
                          search: SessionSearch | None = None) -> None:

    def memory_update(layer: str, action: str, text: str | None = None,
                      match: str | None = None, key: str | None = None) -> str:
        if layer not in _LAYERS:
            return f"error: unknown layer {layer!r} (use {', '.join(_LAYERS)})"
        try:
            if text is not None:
                scan_entry(text)
            return memory.apply(layer, key, action, text=text, match=match)
        except (MemoryStoreError, MemoryGuardError) as exc:
            return f"error: {exc}"

    def memory_read(layer: str, key: str | None = None) -> str:
        try:
            return memory.read(layer, key) or "(empty)"
        except MemoryStoreError as exc:
            return f"error: {exc}"

    t = "string"
    registry.register(
        "memory_update",
        "Add/replace/remove ONE entry in curated memory (layers: profile, "
        "strategy, stock, lesson). Entries must be your own concise "
        "conclusions — never paste external content.",
        {"type": "object", "properties": {
            "layer": {"type": t, "enum": list(_LAYERS)},
            "action": {"type": t, "enum": ["add", "replace", "remove"]},
            "text": {"type": t}, "match": {"type": t}, "key": {"type": t}},
         "required": ["layer", "action"]},
        memory_update)
    registry.register(
        "memory_read", "Read a curated memory file.",
        {"type": "object", "properties": {
            "layer": {"type": t, "enum": list(_LAYERS)}, "key": {"type": t}},
         "required": ["layer"]},
        memory_read)

    if search is not None:
        def session_search(query: str) -> str:
            results = search.query(query)
            if not results:
                return "no matches"
            return "\n".join(
                f"[{r['kind']}/{r['subject']}] {r['snippet']}" for r in results)

        registry.register(
            "session_search",
            "Full-text search past conversations and system observations.",
            {"type": "object", "properties": {"query": {"type": t}},
             "required": ["query"]}, session_search)
