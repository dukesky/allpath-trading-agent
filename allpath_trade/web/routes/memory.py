from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from allpath_trade.memory.store import LAYER_BUDGETS, MemoryStoreError
from allpath_trade.web.account_ctx import bundle
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

LAYER_TITLES = {"profile": "Profile", "strategy": "Strategy notes",
                "stock": "Stock dossiers", "lesson": "Lessons"}

# MemoryStore.path_for() keeps each of these layers as one file per key,
# under a pluralized subdirectory of memory/{account}/ -- "profile" is the
# only layer that is a single flat file, and the only one that stays shared
# at the memory root regardless of account (shadow-dual-active T2). Mirrors
# the subdir map in allpath_trade/memory/store.py so this page globs the
# same directory the store actually writes to.
_KEYED_SUBDIRS = {"strategy": "strategies", "stock": "stocks", "lesson": "lessons"}


def _layer_sections(c, layer: str | None = None) -> list[dict]:
    sections = []
    layers_to_process = [layer] if layer and layer in LAYER_BUDGETS else LAYER_BUDGETS
    for current_layer in layers_to_process:
        title = LAYER_TITLES.get(current_layer, current_layer)
        if current_layer == "profile":
            sections.append({"title": title, "body": c.memory.read(current_layer)})
            continue
        subdir = c.memory.root / c.memory.account / _KEYED_SUBDIRS[current_layer]
        keys = sorted(p.stem for p in subdir.glob("*.md")) if subdir.exists() else []
        if not keys:
            # Nothing written for this layer yet -- still show the section
            # so the page's shape doesn't change once the agent starts
            # writing to it.
            sections.append({"title": title, "body": ""})
            continue
        for key in keys:
            try:
                body = c.memory.read(current_layer, key)
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
    # shadow-dual-active T5: `bundle(request)` gives this account's own
    # MemoryStore -- `.account` scopes the strategy/stock/lesson layers to
    # this account's subdirectory (memory/{account}/...), while `.read
    # ("profile")` still resolves to the shared root file regardless of
    # account (MemoryStore.path_for, per spec: profile stays shared).
    b = bundle(request)
    tab = request.query_params.get("tab", "profile")

    # Unknown tabs fall back to profile
    if tab not in ["profile", "strategy", "stock", "lesson", "changes"]:
        tab = "profile"

    # Build layers based on active tab
    if tab == "changes":
        layers = []
        log = b.memory.recent_log(limit=30)
    else:
        layers = _layer_sections(b, tab)
        log = []

    return templates.TemplateResponse(request, "memory.html", {
        "page": "memory",
        "layers": layers,
        "log": log,
        "active_tab": tab,
        **nav_context(request),
    })
