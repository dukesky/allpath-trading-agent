from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from allpath_trade.memory.store import LAYER_BUDGETS, MemoryStoreError
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

LAYER_TITLES = {"profile": "Profile", "strategy": "Strategy notes",
                "stock": "Stock dossiers", "lesson": "Lessons"}

# MemoryStore.path_for() keeps each of these layers as one file per key,
# under a pluralized subdirectory of the memory root -- "profile" is the
# only layer that is a single flat file. Mirrors the subdir map in
# allpath_trade/memory/store.py so this page globs the same directory the
# store actually writes to.
_KEYED_SUBDIRS = {"strategy": "strategies", "stock": "stocks", "lesson": "lessons"}


def _layer_sections(c) -> list[dict]:
    sections = []
    for layer in LAYER_BUDGETS:
        title = LAYER_TITLES.get(layer, layer)
        if layer == "profile":
            sections.append({"title": title, "body": c.memory.read(layer)})
            continue
        subdir = c.memory.root / _KEYED_SUBDIRS[layer]
        keys = sorted(p.stem for p in subdir.glob("*.md")) if subdir.exists() else []
        if not keys:
            # Nothing written for this layer yet -- still show the section
            # so the page's shape doesn't change once the agent starts
            # writing to it.
            sections.append({"title": title, "body": ""})
            continue
        for key in keys:
            try:
                body = c.memory.read(layer, key)
            except MemoryStoreError:
                # A stray file whose stem the store's key pattern rejects
                # (editor backup, sync-tool artifact, manual poking) never
                # came through apply(), which enforces the same pattern on
                # every write. Skip it rather than 500 the whole page.
                continue
            sections.append({"title": f"{title} — {key}", "body": body})
    return sections


@router.get("/memory", response_class=HTMLResponse)
def memory(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    log = list(c.conn.execute(
        "SELECT ts, layer, key, action, after FROM memory_log"
        " ORDER BY id DESC LIMIT 30"))
    return templates.TemplateResponse(request, "memory.html", {
        "page": "memory", "layers": _layer_sections(c), "log": log, **nav_context(c)})
