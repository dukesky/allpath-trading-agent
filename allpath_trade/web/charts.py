from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from allpath_trade.web.format import money

# Pure rendering -- no I/O, no template lookups (same discipline as
# dashboard.py's summarize_strategy). `equity_svg` is assembled entirely
# from formatted Decimal/datetime values the caller already validated (a
# broker adapter's own numeric response), never from a user- or
# LLM-authored string, which is what lets templating.py mark its output
# `Markup`-safe without re-litigating that exception per call site -- see
# the comment on templating.py's `_svg` filter.

_WIDTH = 640
_HEIGHT = 200
_PAD_X = 12
_PAD_Y = 22
_PLACEHOLDER_TEXT = "No history yet"


def _placeholder_svg(width: int, height: int) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{_PLACEHOLDER_TEXT}" class="equity-svg empty">'
        f'<text x="{width / 2:.1f}" y="{height / 2:.1f}" text-anchor="middle" '
        f'class="muted" font-size="13" font-family="var(--mono)">{_PLACEHOLDER_TEXT}</text>'
        f"</svg>"
    )


def equity_svg(points: list[tuple[datetime, Decimal]], width: int = _WIDTH,
                height: int = _HEIGHT) -> str:
    """Inline SVG equity curve: polyline + light area fill, colored by the
    sign of (last - first) using the same --up/--down vocabulary as every
    P/L figure elsewhere in the app -- the one legitimate "direction" use of
    those variables for a value that isn't itself a literal price move.

    Fewer than two points (no history, or a broker failure the caller
    already degraded to `[]`) renders a muted placeholder instead of a
    degenerate/empty chart -- there is no line to draw from a single point.
    """
    if len(points) < 2:
        return _placeholder_svg(width, height)

    values = [float(v) for _, v in points]
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0  # a perfectly flat line still needs a scale to divide by

    n = len(values)
    plot_w = width - 2 * _PAD_X
    plot_h = height - 2 * _PAD_Y

    def x_at(i: int) -> float:
        return _PAD_X + plot_w * i / (n - 1)

    def y_at(v: float) -> float:
        return _PAD_Y + plot_h * (1 - (v - lo) / span)

    coords = [(x_at(i), y_at(v)) for i, v in enumerate(values)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    baseline = height - _PAD_Y
    area = (f"{coords[0][0]:.1f},{baseline:.1f} " + poly +
            f" {coords[-1][0]:.1f},{baseline:.1f}")

    direction = "up" if values[-1] >= values[0] else "down"
    stroke = f"var(--{direction})"
    fill = f"var(--{direction}-soft)"

    first_label = money(points[0][1])
    last_label = money(points[-1][1])

    # First/last always render (bottom-left / top-right corners, matching
    # their x-position on the line). The hi/lo axis labels share those same
    # two corners -- top-left-ish for hi, bottom-left for lo -- so whenever
    # the highest or lowest value IS the first or last point (the common
    # case for a trending, close-to-monotonic curve), its label would land
    # exactly on top of the first/last label showing the same number
    # (`min(values)`/`max(values)` finds the *first* index a value occurs
    # at, so a monotonic series always trips this). Suppress the redundant
    # hi/lo label in that case rather than draw two overlapping strings.
    hi_idx, lo_idx = values.index(hi), values.index(lo)
    show_hi = hi_idx not in (0, n - 1)
    show_lo = lo_idx not in (0, n - 1)

    labels = (
        f'<text x="{coords[0][0]:.1f}" y="{height - 6}" text-anchor="start" '
        f'class="muted" font-size="11" font-family="var(--mono)">{first_label}</text>'
        f'<text x="{coords[-1][0]:.1f}" y="12" text-anchor="end" '
        f'class="muted" font-size="11" font-family="var(--mono)">{last_label}</text>'
    )
    if show_hi:
        hi_label = money(Decimal(str(hi)))
        labels += (f'<text x="{_PAD_X}" y="12" class="muted" font-size="11" '
                   f'font-family="var(--mono)">{hi_label}</text>')
    if show_lo:
        lo_label = money(Decimal(str(lo)))
        labels += (f'<text x="{_PAD_X}" y="{height - 6}" class="muted" font-size="11" '
                   f'font-family="var(--mono)">{lo_label}</text>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Equity from {first_label} to {last_label}" '
        f'class="equity-svg {direction}">'
        f'<polygon points="{area}" fill="{fill}" stroke="none"></polygon>'
        f'<polyline points="{poly}" fill="none" stroke="{stroke}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline>'
        f"{labels}</svg>"
    )


def signed_money(value: Decimal) -> str:
    """`+$1,183.37` / `-$1,183.37` -- money() has no sign, and the bare
    Decimal sign (`-1183.37`) doesn't put the `$` in the right place, so
    this formats the magnitude through money() and prepends the sign itself
    rather than duplicating money()'s comma/decimal formatting here."""
    sign = "-" if value < 0 else "+"
    return f"{sign}{money(abs(value))}"


def signed_pct(value: float) -> str:
    sign = "-" if value < 0 else "+"
    return f"{sign}{abs(value):.2f}%"
