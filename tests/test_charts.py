from datetime import UTC, datetime
from decimal import Decimal

from allpath_trade.web.charts import equity_svg, signed_money, signed_pct
from tests.helpers import assert_english_only


def _pt(day: int, value: str) -> tuple[datetime, Decimal]:
    return (datetime(2026, 8, day, tzinfo=UTC), Decimal(value))


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


def test_equity_svg_output_is_english_only():
    svg = equity_svg([_pt(1, "10000"), _pt(2, "10500")])
    assert_english_only(svg)
    assert_english_only(equity_svg([]))


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
