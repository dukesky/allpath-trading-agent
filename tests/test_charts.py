import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from allpath_trade.web import charts as charts_module
from allpath_trade.web.charts import equity_since_caption, equity_svg, signed_money, signed_pct
from tests.helpers import assert_english_only


def _pt(day: int, value: str) -> tuple[datetime, Decimal]:
    # Noon UTC -- comfortably midday in ET too (UTC-4/-5), so tests that
    # convert to an ET calendar date never have to worry about a midnight-UTC
    # timestamp landing on the *previous* ET calendar day.
    return (datetime(2026, 8, day, 12, 0, tzinfo=UTC), Decimal(value))


def test_equity_svg_empty_renders_placeholder_text():
    svg = equity_svg([])
    assert "No history yet" in svg
    assert "<svg" in svg


def test_equity_svg_single_point_renders_placeholder_text():
    svg = equity_svg([_pt(1, "10000")])
    assert "No history yet" in svg


def test_equity_svg_two_points_renders_a_line():
    svg = equity_svg([_pt(1, "10000"), _pt(2, "10500")])
    assert "<polyline" in svg
    assert "<polygon" in svg
    assert "No history yet" not in svg


def test_equity_svg_y_scaling_is_monotonic_with_value():
    # A strictly increasing equity series must produce strictly decreasing
    # y-coordinates (SVG y grows downward) -- proves the min/max scaling
    # isn't accidentally inverted or flat.
    svg = equity_svg([_pt(1, "10000"), _pt(2, "10500"), _pt(3, "11000")])
    poly_start = svg.index('<polyline points="') + len('<polyline points="')
    poly_end = svg.index('"', poly_start)
    coords = [tuple(float(n) for n in pair.split(","))
              for pair in svg[poly_start:poly_end].split()]
    ys = [y for _, y in coords]
    assert ys == sorted(ys, reverse=True)


def test_equity_svg_sign_up_when_last_gte_first():
    svg = equity_svg([_pt(1, "10000"), _pt(2, "11000")])
    assert 'class="equity-svg up"' in svg


def test_equity_svg_sign_down_when_last_lt_first():
    svg = equity_svg([_pt(1, "11000"), _pt(2, "10000")])
    assert 'class="equity-svg down"' in svg


def test_equity_svg_flat_line_does_not_crash_on_zero_span():
    svg = equity_svg([_pt(1, "10000"), _pt(2, "10000"), _pt(3, "10000")])
    assert "<polyline" in svg


def test_equity_svg_flat_line_centres_vertically_instead_of_pinning_to_the_bottom():
    # Minor 3: a flat series' zero span used to fall back to a divisor of
    # 1.0, which put every point at y == (height - PAD_Y) -- the plot's
    # bottom edge -- rather than reading as the flat line it actually is.
    svg = equity_svg([_pt(1, "10000"), _pt(2, "10000"), _pt(3, "10000")])
    poly_start = svg.index('<polyline points="') + len('<polyline points="')
    poly_end = svg.index('"', poly_start)
    coords = [tuple(float(n) for n in pair.split(","))
              for pair in svg[poly_start:poly_end].split()]
    ys = {y for _, y in coords}
    assert ys == {charts_module._HEIGHT / 2}


def test_equity_svg_interior_min_label_does_not_collide_with_first_point_label():
    # Reproduces Important 2 exactly: the low point (9000) sits at the
    # *middle* index, not the first/last -- its label must anchor to its own
    # x position rather than the hardcoded corner the first-point label
    # already occupies (the old bug: both drawn at the same (PAD_X, height-6)
    # regardless of where the low point actually falls on the line).
    svg = equity_svg([_pt(1, "10000"), _pt(2, "9000"), _pt(3, "10500")])
    texts = re.findall(r'<text x="([\d.]+)"[^>]*>([^<]+)</text>', svg)
    xs_by_label = {label: float(x) for x, label in texts}
    assert "$10,000.00" in xs_by_label  # first point
    assert "$9,000.00" in xs_by_label   # lo label, an interior point
    assert xs_by_label["$9,000.00"] != xs_by_label["$10,000.00"]


def test_equity_svg_direction_follows_explicit_up_param_over_the_line_itself():
    # Reproduces Important 3: the caller's own up/down verdict (typically a
    # headline computed against a different baseline than the raw line, see
    # dashboard.py's equity_period_summary) must win outright, even when it
    # disagrees with what last-vs-first alone would compute.
    svg_forced_down = equity_svg([_pt(1, "10000"), _pt(2, "11000")], up=False)
    assert 'class="equity-svg down"' in svg_forced_down

    svg_forced_up = equity_svg([_pt(1, "11000"), _pt(2, "10000")], up=True)
    assert 'class="equity-svg up"' in svg_forced_up


def test_equity_svg_direction_falls_back_to_last_vs_first_when_up_not_given():
    assert 'class="equity-svg up"' in equity_svg([_pt(1, "10000"), _pt(2, "11000")])
    assert 'class="equity-svg down"' in equity_svg([_pt(1, "11000"), _pt(2, "10000")])


def test_equity_svg_output_is_english_only():
    svg = equity_svg([_pt(1, "10000"), _pt(2, "10500")])
    assert_english_only(svg)
    assert_english_only(equity_svg([]))


# --- Critical 1: SVG text must use `fill`, not `color` (CSS `color` has no
# effect on SVG <text>) -- verified from both ends: the shipped stylesheet
# actually carries the rule, and equity_svg's own markup actually carries
# the classes/elements that rule targets. ------------------------------------

_APP_CSS = Path(__file__).parent.parent / "allpath_trade" / "web" / "static" / "app.css"


def test_app_css_paints_equity_svg_text_with_fill_not_color():
    css = _APP_CSS.read_text()
    assert ".equity-svg text" in css
    assert "fill: var(--muted)" in css
    # `color:` on an `.equity-svg` selector would be the exact bug this
    # fixes reintroduced -- text needs `fill`, and `color` alone is a no-op
    # for SVG <text> regardless of which theme is active.


def test_app_css_drives_equity_svg_line_and_area_colour_from_real_css_not_inline_var():
    css = _APP_CSS.read_text()
    assert ".equity-svg.up polyline" in css and "stroke: var(--up)" in css
    assert ".equity-svg.down polyline" in css and "stroke: var(--down)" in css
    assert ".equity-svg.up .area" in css
    assert ".equity-svg.down .area" in css


def test_equity_svg_carries_the_classes_and_elements_app_css_targets():
    up_svg = equity_svg([_pt(1, "10000"), _pt(2, "11000")])
    assert 'class="equity-svg up"' in up_svg
    assert '<polygon class="area"' in up_svg
    assert "<text" in up_svg
    # No inline var() color left on the presentation attributes -- colour
    # comes entirely from the CSS rules above, keyed off these classes.
    assert 'stroke="var(' not in up_svg
    assert 'fill="var(' not in up_svg

    down_svg = equity_svg([_pt(1, "11000"), _pt(2, "10000")])
    assert 'class="equity-svg down"' in down_svg

    empty_svg = equity_svg([])
    assert 'class="equity-svg empty"' in empty_svg
    assert "<text" in empty_svg


# --- Minor 4: "since <date>" caption --------------------------------------

def test_equity_since_caption_uses_the_first_points_et_calendar_date():
    caption = equity_since_caption([_pt(5, "10000"), _pt(6, "10500")])
    assert caption == "since Aug 05, 2026"


def test_equity_since_caption_none_when_no_history():
    assert equity_since_caption([]) is None


def test_signed_money_positive():
    assert signed_money(Decimal("1183.37")) == "+$1,183.37"


def test_signed_money_negative():
    assert signed_money(Decimal("-1183.37")) == "-$1,183.37"


def test_signed_money_zero_is_positive_sign():
    assert signed_money(Decimal(0)) == "+$0.00"


def test_signed_pct_positive():
    assert signed_pct(1.18) == "+1.18%"


def test_signed_pct_negative():
    assert signed_pct(-1.18) == "-1.18%"
