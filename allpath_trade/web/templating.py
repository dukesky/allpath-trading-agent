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
templates.env.filters["is_recent_submission"] = fmt.is_recent_submission
templates.env.filters["horizon_label"] = fmt.horizon_label
templates.env.filters["thesis_excerpt"] = fmt.thesis_excerpt


def _md(text: str) -> Markup:
    # One of exactly two sanctioned `Markup(...)` call sites in this
    # codebase (see `_svg` below for the other) -- every other phase's
    # review confirmed zero `|safe`/`Markup` usage anywhere else. Scoped as
    # tightly as possible: `render_markdown` itself does the escaping (its
    # very first operation is `markupsafe.escape` on the raw input -- see
    # allpath_trade/web/markdown.py) and only ever adds tags from its own
    # fixed literal set, so wrapping its *output* in `Markup` here just
    # tells Jinja "this string's escaping has already been handled, don't
    # escape it again" -- it does not grant the input itself any new power.
    # If you are tempted to add another `|safe` or `Markup(...)` call
    # anywhere else in this codebase, route through `render_markdown`
    # instead of duplicating this exception.
    return Markup(render_markdown(text))


templates.env.filters["md"] = _md


def _svg(markup: str) -> Markup:
    # The other of exactly two sanctioned `Markup(...)` call sites (see
    # `_md` above). `web/charts.py`'s `equity_svg` assembles its output
    # purely from formatted Decimal/datetime values -- money-formatted
    # equity figures, computed x/y coordinates -- never from a user- or
    # LLM-authored string, so there is nothing here Jinja's autoescape would
    # ever need to protect against. Do not route arbitrary strings through
    # this filter; if the source isn't `equity_svg` (or another function
    # with the same "numbers only" guarantee), it doesn't belong here.
    return Markup(markup)


templates.env.filters["svg"] = _svg
