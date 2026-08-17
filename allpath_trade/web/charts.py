from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from allpath_trade.scheduler import ET
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
    # No `class="muted"` on the <text> -- CSS `color` has no effect on SVG
    # text (it needs `fill`), so that class used to be a no-op that left
    # this rendering black-on-black against the default dark theme. Colour
    # comes purely from app.css's `.equity-svg text { fill: var(--muted) }`,
    # which this <text> matches with no class needed.
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{_PLACEHOLDER_TEXT}" class="equity-svg empty">'
        f'<text x="{width / 2:.1f}" y="{height / 2:.1f}" text-anchor="middle" '
        f'font-size="13" font-family="var(--mono)">{_PLACEHOLDER_TEXT}</text>'
        f"</svg>"
    )


def _clamp_label_x(x: float, width: int) -> float:
    """Keep a text-anchor="middle" label's centre far enough from the
    viewBox edges that the label itself doesn't spill outside it -- the
    hi/lo labels below are anchored to their own point's x, which can sit
    right at PAD_X or width - PAD_X for a point near either end. `margin` is
    a rough half-width for an 11px money string (e.g. "$10,500.00"),
    generous rather than exact since a slightly-too-wide label sitting a
    few px inside the edge is harmless, unlike one actually clipped by it.
    """
    margin = 26
    return max(margin, min(width - margin, x))


def equity_svg(points: list[tuple[datetime, Decimal]], width: int = _WIDTH,
                height: int = _HEIGHT, *, up: bool | None = None) -> str:
    """Inline SVG equity curve: polyline + light area fill. Colour direction
    (the --up/--down vocabulary every P/L figure elsewhere in the app uses)
    is driven purely by CSS classes on the outer <svg> (`.equity-svg.up`/
    `.equity-svg.down` in app.css) -- this function never emits a `var()`
    color inside an SVG presentation attribute, so there is nothing here
    that depends on custom-property resolution working the same way inside
    `fill`/`stroke` as it does in ordinary CSS.

    `up`, when given, is the caller's own up/down verdict (typically the
    same comparison driving a headline change figure above the chart) and
    wins outright -- see dashboard.py's `equity_period_summary`. Without it,
    direction falls back to the sign of (last - first) in `points`. Passing
    the caller's verdict through is what keeps the chart's colour and a
    headline's sign from ever contradicting each other when they're
    computed from two different baselines (headline: live account equity vs
    first point; line: history's own last vs first point).

    Fewer than two points (no history, or a broker failure the caller
    already degraded to `[]`) renders a muted placeholder instead of a
    degenerate/empty chart -- there is no line to draw from a single point.
    """
    if len(points) < 2:
        return _placeholder_svg(width, height)

    values = [float(v) for _, v in points]
    lo, hi = min(values), max(values)
    span = hi - lo

    n = len(values)
    plot_w = width - 2 * _PAD_X
    plot_h = height - 2 * _PAD_Y

    def x_at(i: int) -> float:
        return _PAD_X + plot_w * i / (n - 1)

    def y_at(v: float) -> float:
        if span == 0:
            # A perfectly flat series has no range to scale against --
            # dividing by a fallback span of 1.0 here (the old behaviour)
            # put every point at `v == lo`, i.e. pinned to the plot's
            # bottom edge, rather than reading as the flat line it is.
            # Centre it vertically instead.
            return _PAD_Y + plot_h / 2
        return _PAD_Y + plot_h * (1 - (v - lo) / span)

    coords = [(x_at(i), y_at(v)) for i, v in enumerate(values)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    baseline = height - _PAD_Y
    area = (f"{coords[0][0]:.1f},{baseline:.1f} " + poly +
            f" {coords[-1][0]:.1f},{baseline:.1f}")

    if up is None:
        direction = "up" if values[-1] >= values[0] else "down"
    else:
        direction = "up" if up else "down"

    first_label = money(points[0][1])
    last_label = money(points[-1][1])

    # First/last always render (bottom-left / top-right corners, matching
    # their x-position on the line). The hi/lo axis labels are anchored to
    # their OWN point's x (not a corner fixed to the first/last position) --
    # whenever the highest or lowest value IS the first or last point (the
    # common case for a trending, close-to-monotonic curve), that's the same
    # point, so its label is suppressed entirely rather than drawn a second
    # time at its own position right next to (or under `text-anchor` shifts,
    # exactly on top of) the first/last label showing the same number.
    hi_idx, lo_idx = values.index(hi), values.index(lo)
    show_hi = hi_idx not in (0, n - 1)
    show_lo = lo_idx not in (0, n - 1)

    labels = (
        f'<text x="{coords[0][0]:.1f}" y="{height - 6}" text-anchor="start" '
        f'font-size="11" font-family="var(--mono)">{first_label}</text>'
        f'<text x="{coords[-1][0]:.1f}" y="12" text-anchor="end" '
        f'font-size="11" font-family="var(--mono)">{last_label}</text>'
    )
    if show_hi:
        hi_label = money(Decimal(str(hi)))
        hx = _clamp_label_x(coords[hi_idx][0], width)
        labels += (f'<text x="{hx:.1f}" y="12" text-anchor="middle" '
                   f'font-size="11" font-family="var(--mono)">{hi_label}</text>')
    if show_lo:
        lo_label = money(Decimal(str(lo)))
        lx = _clamp_label_x(coords[lo_idx][0], width)
        labels += (f'<text x="{lx:.1f}" y="{height - 6}" text-anchor="middle" '
                   f'font-size="11" font-family="var(--mono)">{lo_label}</text>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Equity from {first_label} to {last_label}" '
        f'class="equity-svg {direction}">'
        f'<polygon class="area" points="{area}" stroke="none"></polygon>'
        f'<polyline points="{poly}" fill="none" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline>'
        f"{labels}</svg>"
    )


def equity_since_caption(points: list[tuple[datetime, Decimal]]) -> str | None:
    """"since Aug 05, 2026" -- the ET calendar date of the earliest point in
    `points`, for a caption under the chart. A young account's history can
    be shorter than the selected range's lookback window (or even too short
    for `equity_svg` to draw a line at all -- as little as a single point),
    which otherwise leaves the chart looking wrong with no explanation for
    why; this spells out where the data actually starts. `None` when there
    is no history at all to anchor to. The ET conversion matches every other
    "what calendar day is this" computation in the app (scheduler.py's
    `today_et_date`/`ts_to_et_date`), not the server process's local or UTC
    date. The date is formatted here through a fixed, code-controlled
    strftime pattern -- like `money()`, never from a user- or LLM-authored
    string -- so this stays within the "numbers/ASCII only" contract
    templating.py's `_svg` filter comment describes for `equity_svg`'s own
    output."""
    if not points:
        return None
    first_et = points[0][0].astimezone(ET).date()
    return f"since {first_et.strftime('%b %d, %Y')}"


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
