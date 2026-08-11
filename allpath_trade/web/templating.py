from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from allpath_trade.web import format as fmt
from allpath_trade.web.markdown import render_markdown

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = fmt.money
templates.env.filters["pct"] = fmt.pct
templates.env.filters["ago"] = fmt.ago
templates.env.filters["horizon_label"] = fmt.horizon_label
templates.env.filters["thesis_excerpt"] = fmt.thesis_excerpt


def _md(text: str) -> Markup:
    # THE ONE PLACE `Markup(...)` IS APPLIED IN THIS CODEBASE. Every prior
    # phase's review confirmed zero `|safe`/`Markup` usage anywhere -- this
    # is the first, deliberate exception, and it is scoped as tightly as
    # possible: `render_markdown` itself does the escaping (its very first
    # operation is `markupsafe.escape` on the raw input -- see
    # allpath_trade/web/markdown.py) and only ever adds tags from its own
    # fixed literal set, so wrapping its *output* in `Markup` here just
    # tells Jinja "this string's escaping has already been handled, don't
    # escape it again" -- it does not grant the input itself any new power.
    # If you are tempted to add another `|safe` or `Markup(...)` call
    # anywhere else in this codebase, route through `render_markdown`
    # instead of duplicating this exception.
    return Markup(render_markdown(text))


templates.env.filters["md"] = _md
