"""A minimal, escape-first Markdown-to-HTML renderer for LLM chat replies and
report bodies.

This is the FIRST place in the codebase that produces HTML which a template
is allowed to render unescaped (via the `md` Jinja filter in templating.py,
which wraps this module's output in `Markup` -- see the loud comment there).
Every prior review of this codebase confirmed there was zero `|safe`/`Markup`
anywhere; this module exists to keep that exception bulletproof rather than
to open the door wider.

Escape-first architecture
--------------------------
The VERY FIRST thing `render_markdown` does is HTML-escape the entire input
with `markupsafe.escape`. Every later step -- heading/list/table detection,
the inline bold/italic/code regexes -- runs on that already-escaped text and
only ever wraps *substrings of it* in tags built from fixed literal strings
declared in this module. No user- or model-supplied character is ever
concatenated into the output un-escaped, and no code path here constructs a
tag from anything other than a literal in this file. Concretely: an input of
`<script>alert(1)</script>` becomes the *text* `&lt;script&gt;alert(1)&lt;/
script&gt;` before any Markdown syntax is even looked at, so there is no way
for it to end up as a live `<script>` tag no matter what Markdown-like
characters surround it.

Supported subset
-----------------
Deliberately narrow -- this covers what LLMs actually emit in this app's
chat and report replies, nothing more:

  * `#` .. `####` (and beyond) headings -- shifted down one level so the
    page's own `<h1>` stays unique: 1 `#` -> h2, 2 -> h3, 3 -> h4, 4+ -> h5.
  * `**bold**` and `*italic*`. Underscore-based emphasis (`_x_`, `__x__`) is
    NOT implemented at all, on purpose: tickers like `BRK_B` must never be
    read as markup.
  * `` `inline code` `` -- content is carried through verbatim (already
    escaped) with no further inline parsing inside it.
  * Fenced ``` code blocks -- content is verbatim-escaped text with no
    inline parsing, joined by real newlines inside a single <pre><code>.
    An opening fence's language tag (e.g. "```python") is recognized and
    discarded, not rendered.
  * `-`/`*` bullet lists and `1.` ordered lists. Single level only --
    leading indentation is stripped before matching, so a nested/indented
    list simply flattens into the single enclosing <ul>/<ol> rather than
    nesting or crashing.
  * Pipe tables with a `|---|` separator row. The separator row's cell
    count is the source of truth for the table's width; every other row is
    padded with empty cells or truncated to match, so a row with the wrong
    number of columns renders (oddly) rather than raising.
  * `---` / `***` alone on a line -> `<hr>`.
  * Blank lines separate paragraphs; a single newline inside a paragraph
    becomes `<br>`.

Explicitly NOT supported:

  * Links (`[text](url)`) and images (`![alt](url)`) are NOT parsed --  an
    LLM-authored `href`/`src` is an SSRF/phishing surface this local app has
    no need to expose, so link/image syntax is left as inert literal text
    rather than turned into a clickable/loadable element.
  * No `id`/`class`/`style` (or any other attribute) ever comes from the
    input -- every tag this module emits is a bare tag from the fixed set
    below. Styling is applied by the template's wrapper element, not by
    anything this module writes.
"""

from __future__ import annotations

import re

import markupsafe

# The complete set of tags this module can ever emit. Kept as a real,
# importable constant (rather than only living in a test's hardcoded copy)
# so tests/test_web_markdown.py can assert the corpus never produces
# anything outside it without the list drifting out of sync in two places.
ALLOWED_TAGS = frozenset({
    "p", "br", "h2", "h3", "h4", "h5", "strong", "em", "code", "pre",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "hr",
})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_HR_RE = re.compile(r"^(-{3,}|\*{3,})$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_OL_RE = re.compile(r"^\d+\.\s+(.*)$")

# Inline patterns. All three operate on already-escaped text and only ever
# wrap a captured substring in a literal tag -- they never introduce a new
# `<`/`>`/`&` themselves. `[^\n]` guards keep every span inside one line, in
# keeping with the "single level, no cross-line cleverness" scope above.
_CODE_SPAN_RE = re.compile(r"`([^`\n]+?)`")
_BOLD_RE = re.compile(r"\*\*([^\n]+?)\*\*")
# Applied *after* _BOLD_RE has consumed every `**...**` pair, so a lone `*`
# left over can only be single-emphasis -- the lookaround guards additionally
# stop a stray `*` next to another `*` (e.g. leftover from unbalanced input)
# from being read as italic.
_ITALIC_RE = re.compile(r"(?<!\*)\*([^\n*]+?)\*(?!\*)")

_HEADING_TAG_BY_LEVEL = {1: "h2", 2: "h3", 3: "h4"}
_HEADING_TAG_DEFAULT = "h5"


def _inline(text: str) -> str:
    """Apply bold/italic/code-span formatting to one already-escaped block
    of text (a heading's content, a list item, a table cell, or a joined
    paragraph). Code spans are extracted and stashed first so bold/italic
    markers *inside* them are never treated as formatting -- e.g. a cell
    containing `` `**not bold**` `` must render the asterisks literally."""
    codes: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        # NUL is never produced by markupsafe.escape and never appears in
        # ordinary chat/report text, so it's a safe, simple placeholder --
        # restored below before this function returns.
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_SPAN_RE.sub(_stash, text)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", text)
    return text


def _is_table_separator(line: str) -> bool:
    s = line.strip().strip("|")
    if not s:
        return False
    cells = [c.strip() for c in s.split("|")]
    return all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def _split_row(line: str) -> list[str]:
    s = line.strip().removeprefix("|").removesuffix("|")
    return [c.strip() for c in s.split("|")]


def _render_table(header: list[str], rows: list[list[str]], ncols: int) -> str:
    def pad(cells: list[str]) -> list[str]:
        # Never crash on a row the model got wrong-shaped -- pad short rows
        # with empty cells, silently drop extra ones.
        cells = cells[:ncols]
        cells += [""] * (ncols - len(cells))
        return cells

    head_html = "".join(f"<th>{_inline(c)}</th>" for c in pad(header))
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in pad(row)) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"


def render_markdown(text: str) -> str:
    """Render a narrow Markdown subset to HTML. Returns a plain `str` -- the
    caller (the `md` Jinja filter in templating.py) is the ONLY place that
    wraps this in `Markup` to opt it out of Jinja's autoescaping. Never call
    `Markup(...)` on this function's output anywhere else."""
    escaped = str(markupsafe.escape(text or ""))
    if not escaped.strip():
        return ""

    lines = escaped.split("\n")
    n = len(lines)
    out: list[str] = []
    para_buf: list[str] = []

    def flush_paragraph() -> None:
        if not para_buf:
            return
        joined = "\n".join(para_buf)
        out.append(f"<p>{_inline(joined).replace(chr(10), '<br>')}</p>")
        para_buf.clear()

    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            i += 1  # skip the opening fence line (and any language tag)
            code_lines: list[str] = []
            while i < n and lines[i].strip() != "```":
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1  # skip the closing fence
            # Verbatim -- no _inline() call, so nothing inside a code fence
            # is ever interpreted as Markdown, only ever displayed as text.
            out.append(f"<pre><code>{chr(10).join(code_lines)}</code></pre>")
            continue

        if _HR_RE.match(stripped):
            flush_paragraph()
            out.append("<hr>")
            i += 1
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            tag = _HEADING_TAG_BY_LEVEL.get(level, _HEADING_TAG_DEFAULT)
            out.append(f"<{tag}>{_inline(heading_match.group(2).strip())}</{tag}>")
            i += 1
            continue

        if "|" in line and i + 1 < n and _is_table_separator(lines[i + 1]):
            flush_paragraph()
            header_cells = _split_row(line)
            ncols = len(_split_row(lines[i + 1])) or len(header_cells)
            i += 2
            body_rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                body_rows.append(_split_row(lines[i]))
                i += 1
            out.append(_render_table(header_cells, body_rows, ncols))
            continue

        bullet_match = _BULLET_RE.match(line.lstrip())
        if bullet_match:
            flush_paragraph()
            items = []
            while i < n and (m := _BULLET_RE.match(lines[i].lstrip())):
                items.append(m.group(1).strip())
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + "</ul>")
            continue

        ol_match = _OL_RE.match(line.lstrip())
        if ol_match:
            flush_paragraph()
            items = []
            while i < n and (m := _OL_RE.match(lines[i].lstrip())):
                items.append(m.group(1).strip())
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + "</ol>")
            continue

        if stripped == "":
            flush_paragraph()
            i += 1
            continue

        para_buf.append(line)
        i += 1

    flush_paragraph()
    return "".join(out)
