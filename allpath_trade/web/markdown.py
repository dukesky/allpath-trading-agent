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

Known limitations (accepted, not bugs):

  * There is no way to escape a literal `|` inside a table cell (e.g.
    a backslash-escaped pipe) -- it is always read as a cell separator.
    Rare in LLM output and not worth the added parsing complexity.
  * Overlapping/crossed emphasis markers (e.g. `**bold *crossed** italic*`)
    are not resolved the way a full CommonMark parser would; `_BOLD_RE`
    simply consumes the first `**...**` pair it finds greedily-minimal,
    which can produce a different (but still safe, still escaped) split
    than a spec-compliant parser.
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
# Strips a CommonMark-style closing ATX sequence -- "## Heading ##" should
# read as "Heading", not "Heading ##". Requires at least one space before
# the trailing hashes so a heading whose text genuinely ends in "#" (rare,
# but not impossible) isn't mangled by a hash with no separating space.
_HEADING_TRAILING_HASHES_RE = re.compile(r"\s+#+\s*$")


def _escape_and_normalize(text: str) -> str:
    """The shared escape-first pass both `render_markdown` and
    `to_telegram_html` build on -- see the module docstring's
    "Escape-first architecture" section. Extracted so the Telegram
    formatter reuses this exact escaping instead of re-deriving its own
    (and risking a second, less-audited path from raw input to HTML)."""
    # str(text or "") first: markupsafe.escape() is a no-op on anything
    # implementing __html__ (e.g. a Markup instance) -- see render_markdown's
    # matching comment for why coercing to str first matters.
    escaped = str(markupsafe.escape(str(text or "")))
    # NUL is never legitimate text; stripping it here closes off the
    # placeholder-collision class documented on _inline()'s `\x00{n}\x00`
    # stash below (and, for Telegram output, on `_blocks`'s own stash in the
    # same family further down this module).
    escaped = escaped.replace("\x00", "")
    # Collapse CRLF/CR to a plain LF right after escaping, before any line
    # splitting happens -- see render_markdown's matching comment.
    return re.sub(r"\r\n?", "\n", escaped)


def _inline(text: str) -> str:
    """Apply bold/italic/code-span formatting to one already-escaped block
    of text (a heading's content, a list item, a table cell, or a joined
    paragraph). Code spans are extracted and stashed first so bold/italic
    markers *inside* them are never treated as formatting -- e.g. a cell
    containing `` `**not bold**` `` must render the asterisks literally."""
    codes: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        # NUL is stripped from all escaped input in render_markdown before
        # _inline ever runs (see the `.replace("\x00", "")` there), so this
        # placeholder can never collide with literal input text -- a chat
        # message containing a raw NUL byte (e.g. from a JSON string)
        # used to produce e.g. `\x0099\x00`, which collided with a
        # legitimately-stashed index 99 and either raised IndexError (a
        # PERSISTED message that then 500'd on every future page load) or
        # silently swapped in an unrelated code span's content.
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_SPAN_RE.sub(_stash, text)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", text)

    def _restore(match: re.Match[str]) -> str:
        # Belt-and-braces: the NUL-stripping above should already make this
        # index always valid, but never let a malformed/out-of-range
        # placeholder turn into an unhandled 500 on a persisted message --
        # fall back to the literal matched text instead of raising.
        idx = int(match.group(1))
        if idx >= len(codes):
            return match.group(0)
        return f"<code>{codes[idx]}</code>"

    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return text


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    # A separator row must contain a pipe. Without this, a bare "---" line
    # (already meaningful on its own as an <hr>, see _HR_RE) would make the
    # *previous* prose line -- if it happened to contain any "|" character,
    # e.g. "Compare A | B below" -- look like a table header, silently
    # dropping the rest of that sentence into a fabricated 1-column table.
    if "|" not in s:
        return False
    s = s.strip("|")
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
    # Not reachable today (every caller passes a plain str, and CRLF is rare
    # in LLM output), but closing both structurally costs nothing -- see
    # `_escape_and_normalize`'s own docstring for what each step guards.
    escaped = _escape_and_normalize(text)
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
            # Strip a CommonMark-style closing sequence -- "## a ##" is
            # heading text "a", not "a ##".
            content = _HEADING_TRAILING_HASHES_RE.sub("", heading_match.group(2).strip()).strip()
            out.append(f"<{tag}>{_inline(content)}</{tag}>")
            i += 1
            continue

        if "|" in line and i + 1 < n and _is_table_separator(lines[i + 1]):
            flush_paragraph()
            header_cells = _split_row(line)
            # The separator row's cell count is the source of truth (see the
            # module docstring); `_split_row` on a non-empty stripped string
            # always yields at least one element (str.split never returns
            # []), so there is no zero-cell case here to fall back from.
            ncols = len(_split_row(lines[i + 1]))
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


# ---------------------------------------------------------------------------
# Telegram HTML formatting (Task 2 of the Telegram chat plan -- see
# docs/superpowers/specs/2026-08-12-telegram-chat-design.md §③).
#
# `to_telegram_html` is a second, sibling renderer to `render_markdown`
# above, not a wrapper around it: Telegram's `parse_mode=HTML` only accepts
# a handful of inline tags (https://core.telegram.org/bots/api#html-style)
# and has NO block-level tags at all -- no <p>/<br>/<h*>/<table>/<ul>. So
# this renders the same narrow Markdown subset to a different, smaller
# target: block-level constructs (headings, tables, lists) become inline
# `<b>`/`<pre>` text rather than structural HTML, and blocks are joined with
# a literal blank line the way a human would type a multi-paragraph Telegram
# message. It reuses `_escape_and_normalize` (the exact same escape-first
# pass render_markdown uses) rather than re-deriving its own -- see that
# function's docstring -- so the "escape wins over any Markdown-like
# character" invariant is enforced in exactly one place for both renderers.
# ---------------------------------------------------------------------------

# The complete set of tags to_telegram_html can ever emit -- exported for
# the same reason ALLOWED_TAGS above is: so tests/test_web_markdown.py can
# assert a hostile+feature corpus never produces anything outside it without
# the list living only inside a test file.
TELEGRAM_ALLOWED_TAGS = frozenset({"b", "code", "pre"})


def _tg_inline(text: str) -> str:
    """Bold/code-span formatting for one already-escaped paragraph line,
    Telegram's tag set only (`<b>`/`<code>`, no `<em>`/italic -- Telegram's
    HTML mode has an `<i>` tag, but this app's own Markdown subset only ever
    asks for `**bold**` and `` `code` `` to become Telegram markup; a lone
    `*italic*` stays literal text here exactly like `render_markdown`'s
    `_inline` would produce `<em>`, but there is deliberately no Telegram
    equivalent wired up -- keeping TELEGRAM_ALLOWED_TAGS to exactly
    `{b, code, pre}` as specified). Same code-span-stash-first trick as
    `_inline` above, for the same reason: `**` inside `` `code` `` must stay
    literal, not become `<b>`."""
    codes: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_SPAN_RE.sub(_stash, text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)

    def _restore(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        if idx >= len(codes):
            return match.group(0)
        return f"<code>{codes[idx]}</code>"

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def _tg_render_table(header: list[str], rows: list[list[str]], ncols: int) -> str:
    """Telegram has no `<table>` tag, so a pipe table becomes a single
    `<pre>` block with columns padded to a fixed width -- the closest thing
    to "preserving alignment" available in a monospace-only target. Cell
    content is used verbatim (already escaped, no `_tg_inline` call): a
    `<pre>` block's content must not contain nested tags of its own, or
    Telegram's HTML parser rejects the whole message."""

    def pad(cells: list[str]) -> list[str]:
        cells = cells[:ncols]
        cells += [""] * (ncols - len(cells))
        return cells

    all_rows = [pad(header)] + [pad(r) for r in rows]
    widths = [max(len(row[c]) for row in all_rows) for c in range(ncols)]

    def fmt(row: list[str]) -> str:
        return " | ".join(cell.ljust(widths[c]) for c, cell in enumerate(row))

    lines_out = [fmt(all_rows[0]), "-+-".join("-" * w for w in widths)]
    lines_out.extend(fmt(row) for row in all_rows[1:])
    return "<pre>" + "\n".join(lines_out) + "</pre>"


def to_telegram_html(text: str) -> str:
    """Render the same narrow Markdown subset `render_markdown` understands
    to Telegram's `parse_mode=HTML` dialect. Escape-first, exactly like
    `render_markdown` (see `_escape_and_normalize`): the very first thing
    this does is HTML-escape the whole input, so no user- or model-supplied
    character ever reaches the output un-escaped regardless of what
    Markdown- or HTML-like syntax surrounds it. Output only ever contains
    tags from `TELEGRAM_ALLOWED_TAGS` (`b`, `code`, `pre`), built from fixed
    literal strings in this function -- never from input content.

    Block handling, one call to `flush_paragraph`/`out.append` per block,
    joined with a literal blank line (`"\\n\\n"`) so `split_for_telegram`
    can split on that same boundary:

      * fenced ``` code blocks -> `<pre>` (verbatim, no inline parsing).
      * `#`..`####`+ heading lines -> a `<b>` line (plain escaped text, no
        further inline parsing -- avoids nesting `<b>` inside `<b>`).
      * pipe tables and `-`/`*`/`1.` lists -> `<pre>` blocks (see
        `_tg_render_table` for tables; lists get one item per line, prefixed
        `- `/`N. `). Verbatim, same nested-tag reason as fenced blocks.
      * everything else -> plain text with `**bold**`/`` `code` `` inline
        formatting applied (`_tg_inline`); a bare `---`/`***` divider has no
        Telegram equivalent and is left as the literal escaped text it
        already is (no special-casing, unlike render_markdown's `<hr>`).
    """
    escaped = _escape_and_normalize(text)
    if not escaped.strip():
        return ""

    lines = escaped.split("\n")
    n = len(lines)
    out: list[str] = []
    para_buf: list[str] = []

    def flush_paragraph() -> None:
        if not para_buf:
            return
        out.append(_tg_inline("\n".join(para_buf)))
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
            out.append(f"<pre>{chr(10).join(code_lines)}</pre>")
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            flush_paragraph()
            content = _HEADING_TRAILING_HASHES_RE.sub("", heading_match.group(2).strip()).strip()
            out.append(f"<b>{content}</b>")
            i += 1
            continue

        if "|" in line and i + 1 < n and _is_table_separator(lines[i + 1]):
            flush_paragraph()
            header_cells = _split_row(line)
            ncols = len(_split_row(lines[i + 1]))
            i += 2
            body_rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                body_rows.append(_split_row(lines[i]))
                i += 1
            out.append(_tg_render_table(header_cells, body_rows, ncols))
            continue

        bullet_match = _BULLET_RE.match(line.lstrip())
        if bullet_match:
            flush_paragraph()
            items = []
            while i < n and (m := _BULLET_RE.match(lines[i].lstrip())):
                items.append(m.group(1).strip())
                i += 1
            out.append("<pre>" + "\n".join(f"- {it}" for it in items) + "</pre>")
            continue

        ol_match = _OL_RE.match(line.lstrip())
        if ol_match:
            flush_paragraph()
            items = []
            while i < n and (m := _OL_RE.match(lines[i].lstrip())):
                items.append(m.group(1).strip())
                i += 1
            out.append("<pre>" + "\n".join(f"{idx}. {it}" for idx, it in
                                            enumerate(items, 1)) + "</pre>")
            continue

        if stripped == "":
            flush_paragraph()
            i += 1
            continue

        para_buf.append(line)
        i += 1

    flush_paragraph()
    return "\n\n".join(b for b in out if b)


# `to_telegram_html` joins blocks with "\n\n"; `split_for_telegram` below
# needs to walk back the other way -- given a finished HTML string, find
# those same block boundaries again. A naive `str.split("\n\n")` would be
# wrong: a fenced code block or list can legitimately contain a blank line
# INSIDE it (e.g. a code sample with a blank line for readability), and that
# ends up verbatim inside a `<pre>...</pre>` span. Splitting on every
# "\n\n" without regard for that would slice a chunk boundary through the
# middle of a `<pre>` block, causing a Telegram HTML-parse error (an unclosed tag
# in one chunk, an orphaned `</pre>` in the next). `_PRE_SPAN_RE` finds each
# complete `<pre>...</pre>` span and this stash/restore pass (same trick
# `_inline`'s code-span stashing above uses) protects it from the "\n\n"
# split entirely, treating it as one atomic, un-splittable unit up until
# `_hard_split_block` decides it alone is too big to keep atomic.
_PRE_SPAN_RE = re.compile(r"<pre>.*?</pre>", re.DOTALL)
_PRE_BLOCK_RE = re.compile(r"^<pre>.*</pre>$", re.DOTALL)


def _blocks(html: str) -> list[str]:
    pres: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        pres.append(match.group(0))
        return f"\x00PRE{len(pres) - 1}\x00"

    placeholder = _PRE_SPAN_RE.sub(_stash, html)

    def _restore(block: str) -> str:
        for idx, pre in enumerate(pres):
            block = block.replace(f"\x00PRE{idx}\x00", pre)
        return block

    return [_restore(b) for b in placeholder.split("\n\n")]


def _hard_split_block(block: str, limit: int) -> list[str]:
    """Last-resort split of a single block that alone exceeds `limit` --
    `split_for_telegram` only ever calls this once blank-line-boundary
    splitting has already failed to bring the block under the limit on its
    own. A `<pre>...</pre>` block gets its tags stripped, the inner content
    cut at fixed-size steps sized to leave room for the tags, and each piece
    re-wrapped in its own `<pre>`/`</pre>` -- otherwise a message would open
    a `<pre>` it never closes. A non-`<pre>` block (plain text/`<b>`/`<code>`
    that is, on its own, over 4096 chars) is cut at raw character
    boundaries; there is no tag to re-open, and this shape is not expected
    to occur from `to_telegram_html`'s own paragraph sizes in practice."""
    if _PRE_BLOCK_RE.match(block):
        inner = block[len("<pre>"):-len("</pre>")]
        overhead = len("<pre>") + len("</pre>")
        step = max(1, limit - overhead)
        return [f"<pre>{inner[j:j + step]}</pre>" for j in range(0, len(inner), step)]
    return [block[j:j + limit] for j in range(0, len(block), limit)]


def split_for_telegram(html: str, limit: int = 4096) -> list[str]:
    """Split `to_telegram_html` output into chunks `TelegramAPI.send_message`
    can each send on their own (Telegram's hard cap is 4096 characters per
    message). Greedily packs whole blocks (see `_blocks`) into each chunk up
    to `limit`, splitting only at a blank-line boundary between blocks --
    never inside a `<pre>...</pre>` block -- unless a single block alone
    exceeds `limit`, in which case `_hard_split_block` is the only fallback
    that ever cuts inside one. Returns `[]` for empty input, `[html]`
    unchanged whenever the whole thing already fits (including exactly at
    `limit`)."""
    if not html:
        return []
    if len(html) <= limit:
        return [html]

    blocks = [b for b in _blocks(html) if b]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue
        flush()
        if len(block) <= limit:
            current = block
            continue
        chunks.extend(_hard_split_block(block, limit))

    flush()
    return chunks
