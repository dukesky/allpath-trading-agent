from __future__ import annotations

import re
from datetime import date, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from allpath_trade.scheduler import today_et_date, ts_to_et_date
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

# Same `^\d{4}-\d{2}-\d{2}$` gate the brief calls for -- a `date` path
# param is free-form text until checked; without this, something like
# "../../etc" would reach ReportStore.get as a raw SQL parameter (safe from
# injection, since it's bound, but there is no reason to let a malformed
# value past the point where it obviously can't ever match a real ET
# calendar date).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Caps how much of a tool call's compacted `k=v, k=v` argument text lands in
# the transcript one-liner -- an arg like a full strategy YAML string would
# otherwise blow the "muted one-liner" out into a wall of text.
_TOOL_ARGS_MAX_CHARS = 120


def _is_valid_date(date_str: str) -> bool:
    return bool(_DATE_RE.match(date_str))


def _valid_or_blank(date_str: str) -> bool:
    """A filter bound that's entirely absent ("") means "no restriction on
    this side" -- distinct from a present-but-malformed value, which is the
    actual error case `_is_valid_date` alone would conflate it with."""
    return date_str == "" or _is_valid_date(date_str)


def _chip_dates(c) -> dict[str, str]:
    """Server-computed hrefs for the Today/This week/This month quick
    chips -- "today" here means today's ET calendar date (the same
    calendar reports are keyed by, per ts_to_et_date), not the server
    process's local or UTC date, so the chips agree with what's actually on
    the page even for a server running in another timezone."""
    today = date.fromisoformat(today_et_date())
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    return {
        "today": today.isoformat(),
        "week_start": week_start.isoformat(),
        "month_start": month_start.isoformat(),
    }


def _first_sentence(text: str) -> str:
    """Best-effort first-sentence extraction for the list page's teaser --
    good enough for the plain-prose SUMMARY section reflect.py's report
    prompt requires (see REFLECTION_INSTRUCTIONS), no NLP library needed
    for a one-line list-page preview. A summary with no sentence-ending
    punctuation at all (unusual, but not impossible on a truncated/failed
    row) is shown in full rather than silently dropped."""
    text = (text or "").strip()
    if not text:
        return ""
    match = re.search(r"[.!?](\s|$)", text)
    return text[:match.end()].strip() if match else text


def _proposal_counts(c) -> dict[str, int]:
    """date -> count of strategy_revision rows raised that ET day, derived
    from `ts` (reports are keyed by ET calendar date, pending_reviews rows
    are not) -- shared by both the list page's badge and the detail page's
    proposal block, so the two never disagree about which day a proposal
    belongs to."""
    counts: dict[str, int] = {}
    for row in c.queue.list(None):
        if row["kind"] != "strategy_revision":
            continue
        d = ts_to_et_date(row["ts"])
        if d:
            counts[d] = counts.get(d, 0) + 1
    return counts


def _not_found(request: Request, c, message: str) -> HTMLResponse:
    # Same idiom as strategies.py's `_not_found`: render the index page
    # (with real data, so there's still somewhere to go) at a genuine 404
    # status, rather than a bare HTTPException dropping out of the app
    # chrome.
    reports = c.reports.list()
    return templates.TemplateResponse(request, "reports.html", {
        "page": "reports", "reports": reports,
        "summaries": {r["date"]: _first_sentence(r["summary"]) for r in reports},
        "proposal_counts": _proposal_counts(c),
        "error": message, "notice": None,
        "date_filter": "", "from_filter": "", "to_filter": "",
        **_chip_dates(c), **nav_context(c)}, status_code=404)


@router.get("/reports", response_class=HTMLResponse)
def index(request: Request, date: str = "",
          from_: str = Query("", alias="from"), to: str = "") -> HTMLResponse:
    c = request.app.state.holder.get()
    notice = None
    status_code = 200

    if date:
        # A single-day jump: straight to the detail page when a report
        # exists for that date, otherwise the list with a muted "no report"
        # notice -- either way `date` itself must be well-formed first, or
        # ReportStore.get would just be asked for a date that can never
        # match anything real (see reports/{date}'s own _is_valid_date gate).
        if not _is_valid_date(date):
            notice = f"Invalid date: {date}"
            status_code = 400
        elif c.reports.exists(date):
            return RedirectResponse(f"/reports/{date}", status_code=303)
        else:
            notice = f"No report for {date}"
        reports = c.reports.list()
    elif from_ or to:
        if not _valid_or_blank(from_) or not _valid_or_blank(to):
            notice = "Invalid date range"
            status_code = 400
            reports = c.reports.list()
        else:
            reports = c.reports.list_between(from_ or "0000-01-01", to or "9999-12-31")
    else:
        reports = c.reports.list()

    return templates.TemplateResponse(request, "reports.html", {
        "page": "reports", "reports": reports,
        "summaries": {r["date"]: _first_sentence(r["summary"]) for r in reports},
        "proposal_counts": _proposal_counts(c),
        "error": request.query_params.get("error"), "notice": notice,
        "date_filter": date, "from_filter": from_, "to_filter": to,
        **_chip_dates(c), **nav_context(c)}, status_code=status_code)


@router.get("/reports/{date}", response_class=HTMLResponse)
def detail(request: Request, date: str) -> HTMLResponse:
    c = request.app.state.holder.get()
    if not _is_valid_date(date):
        return _not_found(request, c, "Report not found")
    report = c.reports.get(date)
    if report is None:
        return _not_found(request, c, "Report not found")
    proposals = [dict(r) for r in c.queue.list(None)
                if r["kind"] == "strategy_revision" and ts_to_et_date(r["ts"]) == date]
    return templates.TemplateResponse(request, "report_detail.html", {
        "page": "reports", "report": report, "proposals": proposals,
        "error": request.query_params.get("error"), **nav_context(c)})


def _format_tool_args(arguments: dict) -> str:
    text = ", ".join(f"{k}={v}" for k, v in arguments.items())
    if len(text) > _TOOL_ARGS_MAX_CHARS:
        text = text[:_TOOL_ARGS_MAX_CHARS - 1].rstrip() + "…"
    return text


def _format_result_size(content: object) -> str:
    # A tool result's full content is the report's job to surface (it's
    # already been read, digested, and written into the report body by the
    # reflection agent) -- the transcript replay only needs to prove a
    # result of roughly this size came back, not reproduce it.
    text = content if isinstance(content, str) else str(content)
    length = len(text)
    if length < 1000:
        return f"{length} chars"
    return f"{length / 1000:.1f}k chars"


def _render_turns(messages: list[dict]) -> list[dict]:
    """Flattens stored conversation turns into render-ready blocks: plain
    text bubbles for user/assistant content, one muted line per tool call
    (`→ name(k=v, ...)`), one muted line per tool result (char count
    only). All text fields are plain strings -- the template's default
    Jinja autoescaping (no `|safe` anywhere on this page) is what actually
    keeps this read-only replay from ever rendering stored content as
    markup."""
    turns: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = m.get("content") or ""
            if content:
                turns.append({"role": "user", "kind": "text", "text": content})
        elif role == "assistant":
            text = m.get("content") or ""
            if text:
                turns.append({"role": "assistant", "kind": "text", "text": text})
            for call in m.get("tool_calls") or []:
                name = call.get("name", "")
                args = call.get("arguments") or {}
                line = f"→ {name}({_format_tool_args(args)})"
                turns.append({"role": "assistant", "kind": "tool_call", "text": line})
        elif role == "tool":
            line = f"(result: {_format_result_size(m.get('content'))})"
            turns.append({"role": "tool", "kind": "tool_result", "text": line})
    return turns


@router.get("/reports/{date}/transcript", response_class=HTMLResponse)
def transcript(request: Request, date: str) -> HTMLResponse:
    c = request.app.state.holder.get()
    if not _is_valid_date(date):
        return _not_found(request, c, "Report not found")
    report = c.reports.get(date)
    if report is None:
        return _not_found(request, c, "Report not found")
    turns: list[dict] = []
    if report["conversation_id"] is not None:
        conversations = ConversationStore(c.conn)
        turns = _render_turns(conversations.history(report["conversation_id"]))
    return templates.TemplateResponse(request, "report_transcript.html", {
        "page": "reports", "report": report, "turns": turns, **nav_context(c)})
