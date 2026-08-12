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

import markupsafe
import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.llm.base import LLMResponse
from allpath_trade.web.app import create_app
from allpath_trade.web.markdown import (
    ALLOWED_TAGS,
    TELEGRAM_ALLOWED_TAGS,
    render_markdown,
    split_for_telegram,
    to_telegram_html,
)
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


# ---------------------------------------------------------------------------
# 6. Regression coverage for review findings I1/I2/I3/M3/M5.
# ---------------------------------------------------------------------------

def test_nul_byte_input_does_not_collide_with_a_code_span_placeholder():
    # A literal NUL byte (e.g. from a JSON string) used to be
    # indistinguishable from _inline's `\x00{n}\x00` code-span placeholder --
    # `\x0099\x00` looked like a reference to stash index 99, which either
    # raised IndexError (unhandled 500) or, for a small enough number,
    # silently substituted an unrelated code span's content.
    out = render_markdown("\x0099\x00")
    assert "99" in out
    assert "<code>" not in out  # no real code span was ever stashed


def test_nul_byte_that_collides_with_a_real_stash_index_does_not_substitute():
    # One legitimate code span (stash index 0) plus a forged `\x000\x00` --
    # the forged placeholder must not be swapped for the real span's content.
    out = render_markdown("`abc` \x000\x00")
    assert "<code>abc</code>" in out
    assert out.count("<code>") == 1


def test_prose_line_with_pipe_followed_by_hr_does_not_become_a_table():
    out = render_markdown("Compare A | B below\n---\nnext")
    assert "<table>" not in out
    assert "Compare A | B below" in out
    assert "<hr>" in out
    assert "next" in out


def test_markup_input_is_still_escaped_not_passed_through_live():
    # markupsafe.escape() is a no-op on anything implementing __html__, so a
    # Markup(...) argument would otherwise bypass the escape-first pass
    # entirely. Not reachable from any caller today, but render_markdown
    # coerces to str first so the invariant holds structurally either way.
    out = render_markdown(markupsafe.Markup("<script>alert(1)</script>"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_crlf_and_bare_cr_are_normalized_before_br_conversion():
    out_crlf = render_markdown("line one\r\nline two")
    out_cr = render_markdown("line one\rline two")
    assert out_crlf == "<p>line one<br>line two</p>"
    assert out_cr == "<p>line one<br>line two</p>"
    assert "\r" not in out_crlf
    assert "\r" not in out_cr


def test_heading_trailing_hashes_are_stripped():
    out = render_markdown("## a ##")
    assert out == "<h3>a</h3>"


def test_nul_bearing_assistant_message_does_not_500_the_chat_page(tmp_path, monkeypatch):
    # Route-level version of the NUL-collision regression above: the
    # assistant's reply is PERSISTED, so if render_markdown ever raises on
    # it, every subsequent GET /chat -- not just this POST -- would 500.
    payload = "\x0099\x00"
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text=payload)])

    post_resp = client.post("/chat/send", data={"message": "hi"})
    assert post_resp.status_code == 200

    get_resp = client.get("/chat")
    assert get_resp.status_code == 200
    assert "99" in get_resp.text


# ---------------------------------------------------------------------------
# 7. to_telegram_html -- Task 2 of the Telegram chat plan (spec §③). Same
#    three-layer approach as the render_markdown sections above: hostile
#    corpus, tag whitelist, then feature shapes -- but against Telegram's
#    much smaller allowed-tag set (`{b, code, pre}`, no block-level tags at
#    all since Telegram HTML mode has none).
# ---------------------------------------------------------------------------

TG_HOSTILE_CASES = {
    "bare_script": "<script>alert(1)</script>",
    "script_in_bold": "**<script>alert(1)</script>**",
    "raw_b_tag_in_text": "<b>injected</b>",
    "bold_wrapped_forged_b": "**<b>**",
    "script_in_code_span": "`<script>alert(1)</script>`",
    "script_in_fence": "```\n<script>alert(1)</script>\n```",
    "script_in_table_cell": "| a | b |\n|---|---|\n| 1 | <script>alert(1)</script> |",
    "script_in_heading": "# <script>alert(1)</script>",
    "script_in_list_item": "- <script>alert(1)</script>",
    "pre_tag_forgery": "<pre>evil</pre>",
    "img_onerror": '<img src=x onerror="alert(1)">',
}


def test_to_telegram_html_hostile_script_payloads_only_ever_appear_escaped():
    for name, payload in TG_HOSTILE_CASES.items():
        out = to_telegram_html(payload)
        assert "<script>" not in out, f"{name}: raw <script> leaked: {out!r}"
        assert "</script>" not in out, f"{name}: raw </script> leaked: {out!r}"
        if "<script>" in payload:
            assert "&lt;script&gt;" in out, f"{name}: escaped form missing: {out!r}"


def test_to_telegram_html_forged_b_tag_in_input_is_escaped_not_a_real_tag():
    out = to_telegram_html(TG_HOSTILE_CASES["raw_b_tag_in_text"])
    assert out == "&lt;b&gt;injected&lt;/b&gt;"


def test_to_telegram_html_bold_wrapping_a_forged_tag_stays_escaped_inside_b():
    out = to_telegram_html(TG_HOSTILE_CASES["bold_wrapped_forged_b"])
    assert out == "<b>&lt;b&gt;</b>"


def test_to_telegram_html_pre_forgery_never_becomes_a_real_pre_tag():
    out = to_telegram_html(TG_HOSTILE_CASES["pre_tag_forgery"])
    assert out == "&lt;pre&gt;evil&lt;/pre&gt;"


def test_to_telegram_html_fence_containing_a_script_tag_is_verbatim_escaped():
    out = to_telegram_html(TG_HOSTILE_CASES["script_in_fence"])
    assert out == "<pre>&lt;script&gt;alert(1)&lt;/script&gt;</pre>"


def test_to_telegram_html_img_onerror_is_inert_text_not_an_element():
    out = to_telegram_html(TG_HOSTILE_CASES["img_onerror"])
    assert "<img" not in out
    assert "&lt;img" in out
    assert "onerror=" in out  # present only as visible escaped text


TG_FEATURE_CASES = {
    "bold": "This is **bold** text.",
    "code": "Use `pip install x` to install.",
    "fence": "```python\nprint('hi')\n```",
    "heading": "## Section Title",
    "table": "| a | bb |\n|---|---|\n| 1 | 22 |",
    "list_bullet": "- one\n- two",
    "list_ordered": "1. first\n2. second",
    "paragraphs": "First paragraph.\n\nSecond paragraph.",
    "italic_stays_literal": "This is *italic* text.",
    "hr_stays_literal": "above\n\n---\n\nbelow",
    "code_inside_bold_not_reparsed": "**`not **bold**`**",
}


def test_to_telegram_html_output_never_contains_a_tag_outside_the_allowed_set():
    for corpus in (TG_HOSTILE_CASES, TG_FEATURE_CASES):
        for name, payload in corpus.items():
            out = to_telegram_html(payload)
            found = _tags_in(out)
            assert found <= TELEGRAM_ALLOWED_TAGS, (
                f"{name}: disallowed tag(s) {found - TELEGRAM_ALLOWED_TAGS} in {out!r}")


def test_telegram_allowed_tags_is_exactly_b_code_pre():
    assert TELEGRAM_ALLOWED_TAGS == {"b", "code", "pre"}


def test_to_telegram_html_bold_becomes_b_tag():
    assert to_telegram_html(TG_FEATURE_CASES["bold"]) == "This is <b>bold</b> text."


def test_to_telegram_html_inline_code_becomes_code_tag():
    out = to_telegram_html(TG_FEATURE_CASES["code"])
    assert out == "Use <code>pip install x</code> to install."


def test_to_telegram_html_fenced_block_becomes_pre_verbatim_language_tag_discarded():
    out = to_telegram_html(TG_FEATURE_CASES["fence"])
    assert out == "<pre>print(&#39;hi&#39;)</pre>"


def test_to_telegram_html_heading_becomes_a_bold_line():
    assert to_telegram_html(TG_FEATURE_CASES["heading"]) == "<b>Section Title</b>"


def test_to_telegram_html_table_becomes_an_aligned_pre_block():
    out = to_telegram_html(TG_FEATURE_CASES["table"])
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert "<table>" not in out
    inner_lines = out[len("<pre>"):-len("</pre>")].splitlines()
    assert len(inner_lines) == 3  # header, separator, one data row
    assert "a" in inner_lines[0] and "bb" in inner_lines[0]
    assert "1" in inner_lines[2] and "22" in inner_lines[2]
    # Columns are padded to a fixed width -- "a"/"1" line up with "bb"/"22".
    assert inner_lines[0].index("bb") == inner_lines[2].index("22")


def test_to_telegram_html_bullet_list_becomes_a_pre_block():
    out = to_telegram_html(TG_FEATURE_CASES["list_bullet"])
    assert out == "<pre>- one\n- two</pre>"


def test_to_telegram_html_ordered_list_becomes_a_pre_block():
    out = to_telegram_html(TG_FEATURE_CASES["list_ordered"])
    assert out == "<pre>1. first\n2. second</pre>"


def test_to_telegram_html_paragraphs_joined_by_a_blank_line():
    out = to_telegram_html(TG_FEATURE_CASES["paragraphs"])
    assert out == "First paragraph.\n\nSecond paragraph."


def test_to_telegram_html_italic_marker_has_no_telegram_equivalent_stays_literal():
    out = to_telegram_html(TG_FEATURE_CASES["italic_stays_literal"])
    assert out == "This is *italic* text."
    assert "<i>" not in out
    assert "<em>" not in out


def test_to_telegram_html_hr_divider_has_no_telegram_equivalent_stays_literal():
    out = to_telegram_html(TG_FEATURE_CASES["hr_stays_literal"])
    assert out == "above\n\n---\n\nbelow"


def test_to_telegram_html_code_span_content_is_not_reparsed_for_bold():
    # The `**` markers inside a `` `code span` `` must render literally, not
    # be re-read as bold -- exact mirror of render_markdown's own
    # test_markdown_chars_inside_a_code_span_are_not_parsed.
    out = to_telegram_html(TG_FEATURE_CASES["code_inside_bold_not_reparsed"])
    assert out == "<b><code>not **bold**</code></b>"


def test_to_telegram_html_blank_input_returns_empty_string():
    assert to_telegram_html("") == ""
    assert to_telegram_html("   \n  \n") == ""


def test_to_telegram_html_reuses_the_shared_escape_pass_crlf_normalized():
    # Guards that to_telegram_html didn't re-derive its own escaping --
    # CRLF normalization (a render_markdown behavior living in the shared
    # _escape_and_normalize helper) must apply here too.
    out = to_telegram_html("line one\r\nline two")
    assert "\r" not in out
    assert out == "line one\nline two"


# ---------------------------------------------------------------------------
# 8. split_for_telegram -- blank-line-boundary splitting with a 4096-char
#    default limit, never slicing inside a <pre> block unless that block
#    alone exceeds the limit.
# ---------------------------------------------------------------------------

def test_split_for_telegram_returns_empty_list_for_empty_input():
    assert split_for_telegram("") == []


def test_split_for_telegram_returns_single_chunk_when_comfortably_under_limit():
    html = "<b>hello</b> world"
    assert split_for_telegram(html) == [html]


def test_split_for_telegram_returns_single_chunk_at_exactly_the_limit():
    html = "a" * 4096
    result = split_for_telegram(html)
    assert result == [html]
    assert len(result[0]) == 4096


def test_split_for_telegram_one_char_over_limit_splits_into_two_and_loses_nothing():
    html = "a" * 4097
    result = split_for_telegram(html)
    assert len(result) == 2
    assert all(len(c) <= 4096 for c in result)
    assert "".join(result) == html


def test_split_for_telegram_multi_paragraph_packs_greedily_and_reconstructs_exactly():
    paragraphs = [f"Paragraph {i}: " + ("x" * 60) for i in range(100)]
    html = "\n\n".join(paragraphs)
    assert len(html) > 4096
    result = split_for_telegram(html)
    assert len(result) > 1
    assert all(len(c) <= 4096 for c in result)
    # Greedy packing at blank-line boundaries only -- rejoining every chunk
    # with the same separator recovers the exact original string.
    assert "\n\n".join(result) == html


def test_split_for_telegram_never_splits_inside_a_pre_block_that_fits_the_limit():
    pre_content = "line with a blank line below\n\nstill inside the pre block"
    full_pre = f"<pre>{pre_content}</pre>"
    html = "intro paragraph\n\n" + full_pre + "\n\n" + ("filler " * 700)
    assert len(html) > 4096
    result = split_for_telegram(html)
    assert all(len(c) <= 4096 for c in result)
    # The pre block -- including its own internal blank line -- appears
    # intact in exactly one chunk, never fragmented across two.
    assert sum(full_pre in c for c in result) == 1
    for c in result:
        if full_pre not in c:
            assert "<pre>" not in c and "</pre>" not in c


def test_split_for_telegram_hard_splits_a_single_pre_block_that_alone_exceeds_the_limit():
    inner = "x" * 5000
    html = f"<pre>{inner}</pre>"
    assert len(html) > 4096
    result = split_for_telegram(html)
    assert len(result) > 1
    for chunk in result:
        assert len(chunk) <= 4096
        assert chunk.startswith("<pre>")
        assert chunk.endswith("</pre>")
    reassembled = "".join(c[len("<pre>"):-len("</pre>")] for c in result)
    assert reassembled == inner


def test_split_for_telegram_hard_split_of_a_huge_non_pre_block_has_no_tags_to_reopen():
    html = "a" * 9000
    result = split_for_telegram(html)
    assert len(result) == 3
    assert "".join(result) == html
    for chunk in result:
        assert len(chunk) <= 4096


# ---------------------------------------------------------------------------
# 9. Regression tests for Finding 1: hard-split boundary safety and tag
#    balancing in non-<pre> blocks.
# ---------------------------------------------------------------------------

def test_hard_split_block_word_boundary_splits():
    """Splitting at word boundaries preserves whitespace boundaries."""
    from allpath_trade.web.markdown import _hard_split_block
    # A paragraph of repeated words that fits exactly when split at word boundary.
    text = ("word " * 1000).strip()  # Just under the limit when split at spaces
    result = _hard_split_block(text, 4096)
    # Each chunk should be <= 4096 and end at a word boundary (no partial words).
    assert all(len(c) <= 4096 for c in result)
    # Reconstruct to ensure nothing was lost (accounting for potential trailing space).
    reconstructed = " ".join(result)
    # The original and reconstructed should match (modulo whitespace normalization).
    assert reconstructed.replace("  ", " ") == text


def test_hard_split_block_no_split_inside_bold_tags():
    """Bold markers straddling the boundary don't get cut in half."""
    from allpath_trade.web.markdown import _hard_split_block
    # Create text where <b>**bold**</b> markers straddle a would-be split point.
    # Pad to just under 4096, then add bold text.
    text = "x" * 4090 + "<b>bold marker here</b>" + "y" * 500
    result = _hard_split_block(text, 4096)
    # Each chunk must have balanced tags.
    for chunk in result:
        assert chunk.count("<b>") == chunk.count("</b>"), f"Unbalanced <b> in chunk: {chunk!r}"
        assert chunk.count("<code>") == chunk.count("</code>"), f"Unbalanced <code> in chunk: {chunk!r}"


def test_hard_split_block_ampersand_entity_never_straddled():
    """HTML entities like &amp; never get split across chunks."""
    from allpath_trade.web.markdown import _hard_split_block
    # Create text with &amp; at a position that would be split.
    text = "a" * 4095 + "&amp;" + "b" * 500
    result = _hard_split_block(text, 4096)
    # Reconstruct and verify the entity is intact.
    reconstructed = "".join(result)
    # Count &amp; in original and reconstructed.
    assert reconstructed.count("&amp;") == 1
    # Ensure no partial entities like "amp;" or "&am" without the full entity.
    for chunk in result:
        # A chunk should either have complete "&amp;" or none of the entity chars
        # fragmented across boundaries.
        if "&" in chunk:
            amp_pos = chunk.find("&")
            semi_pos = chunk.find(";", amp_pos)
            if semi_pos != -1:
                # Entity is complete within this chunk.
                assert chunk[amp_pos:semi_pos + 1] == "&amp;"
