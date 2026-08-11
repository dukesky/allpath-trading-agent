"""Adversarial coverage for allpath_trade/web/markdown.py -- this is the
seam that guards the codebase's first sanctioned `Markup(...)` call (see
templating.py's `_md`). Every review of this codebase before this module
existed confirmed zero `|safe`/`Markup` anywhere; these tests exist to keep
that exception bulletproof, not to relax it.

Three layers, in order: (1) unit-level hostile-corpus tests directly against
`render_markdown`, (2) a whitelist test that the full corpus (hostile +
feature) never emits a tag outside markdown.py's own ALLOWED_TAGS constant,
(3) template-level tests confirming the `md` filter actually reaches the
chat and report templates -- and that user-typed text does NOT."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.llm.base import LLMResponse
from allpath_trade.web.app import create_app
from allpath_trade.web.markdown import ALLOWED_TAGS, render_markdown
from tests.test_sentinel import FakeBroker
from tests.test_web_chat import make_client
from tests.test_web_reports import add_report

_TAG_RE = re.compile(r"</?([a-z0-9]+)")


@pytest.fixture
def reports_client(tmp_path, monkeypatch):
    # Mirrors tests/test_web_reports.py's `client` fixture -- kept local
    # rather than imported so this file follows the same
    # every-test-module-owns-its-fixture convention the rest of tests/
    # already uses (see test_web_app.py, test_web_reviews.py, etc.), and so
    # ruff doesn't flag a cross-module fixture import as a redefinition.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def _tags_in(html: str) -> set[str]:
    return {m.group(1) for m in _TAG_RE.finditer(html)}


# ---------------------------------------------------------------------------
# 1. Hostile corpus -- every case must have its payload land ONLY escaped.
# ---------------------------------------------------------------------------

HOSTILE_CASES = {
    "bare_script": "<script>alert(1)</script>",
    "script_in_bold": "**<script>alert(1)</script>**",
    "script_in_table_cell": (
        "| a | b |\n|---|---|\n| 1 | <script>alert(1)</script> |"
    ),
    "script_in_code_fence": "```\n<script>alert(1)</script>\n```",
    "img_onerror": '<img src=x onerror="alert(1)">',
    "svg_onload_breakout": '"><svg/onload=alert(1)>',
    "markdown_chars_in_code_span": "`**not bold** | not a table | # not a heading`",
    "javascript_link": "[click](javascript:alert(1))",
    "bold_wrapped_tag": "**<b>**",
}


def test_hostile_script_payloads_only_ever_appear_escaped():
    for name, payload in HOSTILE_CASES.items():
        out = render_markdown(payload)
        assert "<script>" not in out, f"{name}: raw <script> leaked into output: {out!r}"
        assert "</script>" not in out, f"{name}: raw </script> leaked into output: {out!r}"
        if "<script>" in payload:
            assert "&lt;script&gt;" in out, f"{name}: escaped form missing: {out!r}"


def test_img_onerror_is_inert_text_not_an_element():
    out = render_markdown(HOSTILE_CASES["img_onerror"])
    assert "<img" not in out
    assert "onerror=" in out  # present, but as visible escaped text
    assert "&lt;img" in out


def test_svg_onload_breakout_cannot_close_a_parent_tag():
    out = render_markdown(HOSTILE_CASES["svg_onload_breakout"])
    assert "<svg" not in out
    assert "&lt;svg" in out
    # The leading `">` must not have been able to close anything real --
    # it's escaped along with the rest.
    assert "&gt;&lt;svg" in out or "&quot;&gt;&lt;svg" in out


def test_markdown_chars_inside_a_code_span_are_not_parsed():
    out = render_markdown(HOSTILE_CASES["markdown_chars_in_code_span"])
    assert "<strong>" not in out
    assert "<h" not in out
    assert "<code>" in out
    # The literal asterisks/pipe/hash from inside the span survive as text.
    assert "**not bold**" in out
    assert "not a table" in out
    assert "not a heading" in out


def test_javascript_link_renders_as_literal_text_no_anchor():
    out = render_markdown(HOSTILE_CASES["javascript_link"])
    assert "<a " not in out
    assert "<a>" not in out
    assert "href" not in out
    # The full literal source text is visible, URL included.
    assert "[click](javascript:alert(1))" in out


def test_bold_wrapping_a_forged_tag_stays_escaped_inside_strong():
    out = render_markdown(HOSTILE_CASES["bold_wrapped_tag"])
    assert out == "<p><strong>&lt;b&gt;</strong></p>"


def test_no_images_ever_rendered():
    out = render_markdown("![alt](http://evil.example/x.png)")
    assert "<img" not in out
    assert "![alt]" in out


# ---------------------------------------------------------------------------
# 2. Output tag whitelist -- hostile corpus + feature corpus combined.
# ---------------------------------------------------------------------------

FEATURE_CASES = {
    "screenshot_shape": "## ✅ TSLA 已成交!\n\n"
                         "| Field | Value |\n|---|---|\n"
                         "| **Side** | buy |\n| **Qty** | 10 |\n\n---\n\nDone.",
    "headings": "# H1-ish\n## H2-ish\n### H3-ish\n#### H4-ish\n##### H5-ish",
    "lists": "- one\n- two\n* three\n\n1. first\n2. second",
    "hr_variants": "above\n\n---\n\nmiddle\n\n***\n\nbelow",
    "mixed_inline": "This is **bold**, this is *italic*, this is `code`, and BRK_B stays put.",
    "ragged_table": "| a | b | c |\n|---|---|---|\n| only-one |\n| way | too | many | cells |",
    "unterminated_fence": "```\nno closing fence\nmore text",
    "plain_prose": "Just a normal sentence with no markdown at all.",
}


def test_output_never_contains_a_tag_outside_the_allowed_set():
    for corpus in (HOSTILE_CASES, FEATURE_CASES):
        for name, payload in corpus.items():
            out = render_markdown(payload)
            found = _tags_in(out)
            assert found <= ALLOWED_TAGS, f"{name}: disallowed tag(s) {found - ALLOWED_TAGS} in {out!r}"


def test_allowed_tags_matches_the_documented_set():
    # Guards against ALLOWED_TAGS silently drifting from the spec'd set.
    assert ALLOWED_TAGS == {
        "p", "br", "h2", "h3", "h4", "h5", "strong", "em", "code", "pre",
        "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "hr",
    }


# ---------------------------------------------------------------------------
# 3. Feature cases -- the shapes the user's screenshot actually showed.
# ---------------------------------------------------------------------------

def test_heading_levels_shift_down_one_so_page_h1_stays_unique():
    out = render_markdown(FEATURE_CASES["headings"])
    assert "<h2>H1-ish</h2>" in out
    assert "<h3>H2-ish</h3>" in out
    assert "<h4>H3-ish</h4>" in out
    assert "<h5>H4-ish</h5>" in out
    assert "<h5>H5-ish</h5>" in out
    assert "<h1" not in out


def test_fill_confirmation_table_renders_as_a_real_table():
    out = render_markdown(FEATURE_CASES["screenshot_shape"])
    assert "<table>" in out
    assert "<thead>" in out and "<tbody>" in out
    assert "<th>Field</th>" in out
    assert "<td><strong>Side</strong></td>" in out
    assert "<hr>" in out
    assert "✅ TSLA 已成交!" in out  # CJK/emoji survive as text content


def test_hr_variants_render_as_hr():
    out = render_markdown(FEATURE_CASES["hr_variants"])
    assert out.count("<hr>") == 2


def test_bullet_and_ordered_lists():
    out = render_markdown(FEATURE_CASES["lists"])
    assert "<ul><li>one</li><li>two</li><li>three</li></ul>" in out
    assert "<ol><li>first</li><li>second</li></ol>" in out


def test_bold_italic_code_and_ticker_underscore_is_not_italic():
    out = render_markdown(FEATURE_CASES["mixed_inline"])
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<code>code</code>" in out
    assert "BRK_B" in out
    assert "<em>B" not in out  # the underscore never triggers emphasis


def test_ragged_table_rows_are_padded_or_truncated_never_crash():
    out = render_markdown(FEATURE_CASES["ragged_table"])
    assert "<table>" in out
    # short row padded to 3 cells, long row truncated to 3 cells
    assert out.count("<td>") == 6


def test_unterminated_fence_does_not_crash_and_stays_a_code_block():
    out = render_markdown(FEATURE_CASES["unterminated_fence"])
    assert "<pre><code>" in out
    assert "no closing fence" in out


# ---------------------------------------------------------------------------
# 4. Round-trip sanity.
# ---------------------------------------------------------------------------

def test_plain_prose_becomes_a_single_paragraph():
    out = render_markdown(FEATURE_CASES["plain_prose"])
    assert out == "<p>Just a normal sentence with no markdown at all.</p>"


def test_blank_input_renders_nothing():
    assert render_markdown("") == ""
    assert render_markdown("   \n  \n") == ""


def test_single_newline_inside_a_paragraph_becomes_br():
    out = render_markdown("line one\nline two")
    assert out == "<p>line one<br>line two</p>"


def test_blank_line_starts_a_new_paragraph():
    out = render_markdown("first para\n\nsecond para")
    assert out == "<p>first para</p><p>second para</p>"


# ---------------------------------------------------------------------------
# 5. Template-level: chat assistant vs. user, and report bodies.
# ---------------------------------------------------------------------------

def test_assistant_markdown_renders_elements_but_user_text_with_same_markup_stays_literal(
        tmp_path, monkeypatch):
    # A single "#" -> <h2>; this uses "## Heading" (two hashes), which per
    # markdown.py's documented level shift maps to <h3>.
    md_text = "## Heading\n\n**bold** and a table:\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text=md_text)])

    r = client.post("/chat/send", data={"message": md_text})

    # The assistant's reply (echoing the same markdown-shaped text back)
    # must render as real elements...
    assert "<h3>Heading</h3>" in r.text
    assert "<strong>bold</strong>" in r.text
    assert "<table>" in r.text
    # ...but the user's own bubble, containing byte-for-byte the same
    # source text, must stay literal -- only the assistant branch gets the
    # `md` filter (see _chat_messages.html).
    assert "## Heading" in r.text  # literal, from the user bubble
    assert "**bold**" in r.text  # literal, from the user bubble


def test_existing_assistant_html_escape_test_still_holds(tmp_path, monkeypatch):
    # Guards that the markdown renderer's escape-first pass didn't regress
    # the existing invariant test_web_chat.py already asserts.
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="<script>alert(1)</script>")])
    r = client.post("/chat/send", data={"message": "hi"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_chat_page_client_js_stays_textcontent_only():
    # markdown.py's introduction must not tempt the client-side optimistic
    # echo into innerHTML -- it must keep building the bubble via
    # textContent so the user's own typed text is never a client-side XSS
    # vector (see test_web_chat.py's own version of this assertion; kept
    # here too since this file is the one guarding the |safe seam).
    import pathlib
    chat_html = (pathlib.Path(__file__).parents[1] / "allpath_trade" / "web"
                 / "templates" / "chat.html").read_text()
    assert "innerHTML" not in chat_html
    assert "textContent" in chat_html


def test_report_body_with_markdown_renders_elements(reports_client):
    md_body = "## Summary\n\nPositions look **healthy**.\n\n| Ticker | Weight |\n|---|---|\n| AAPL | 15% |"
    add_report(reports_client, body=md_body)
    r = reports_client.get("/reports/2026-08-10")
    assert "<h3>Summary</h3>" in r.text  # "##" -> h3, see markdown.py's level shift
    assert "<strong>healthy</strong>" in r.text
    assert "<table>" in r.text


def test_report_body_plain_text_stays_plain_paragraphs(reports_client):
    add_report(reports_client, body="REPORT\nbody text")
    r = reports_client.get("/reports/2026-08-10")
    assert "<p>REPORT<br>body text</p>" in r.text
    # No proposals were added for this report, so report_detail.html's own
    # literal "Strategy revision proposals" <h2> doesn't render either --
    # asserting no h2/h3/h4/h5 tag appears at all, not just substring "<h"
    # (which would false-positive on <html>/<head> from base.html).
    for tag in ("h2", "h3", "h4", "h5"):
        assert f"<{tag}>" not in r.text and f"<{tag} " not in r.text
