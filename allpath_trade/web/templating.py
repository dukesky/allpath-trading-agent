from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from allpath_trade.web import format as fmt

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = fmt.money
templates.env.filters["pct"] = fmt.pct
templates.env.filters["ago"] = fmt.ago
templates.env.filters["horizon_label"] = fmt.horizon_label
templates.env.filters["thesis_excerpt"] = fmt.thesis_excerpt
