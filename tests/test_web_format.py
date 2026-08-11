from allpath_trade.web.format import horizon_label, thesis_excerpt

# --- thesis_excerpt ---------------------------------------------------------

def test_thesis_excerpt_empty_is_empty_string():
    assert thesis_excerpt("") == ""
    assert thesis_excerpt(None) == ""


def test_thesis_excerpt_takes_only_the_first_sentence():
    # There's more thesis after the first sentence, so the excerpt is a
    # genuine truncation -- it must say so with a trailing ellipsis.
    text = "Services growth continues. Margin expansion likely next year."
    assert thesis_excerpt(text) == "Services growth continues.…"


def test_thesis_excerpt_takes_first_line_when_no_terminal_punctuation():
    # No '.', '!', or '?' anywhere in the text -- only then is the bare
    # newline (a YAML block-scalar hard-wrap) trusted as a boundary.
    text = "Bullish on cloud momentum without a full stop\nExpansion into new markets continues"
    assert thesis_excerpt(text) == "Bullish on cloud momentum without a full stop…"


def test_thesis_excerpt_whole_text_when_no_sentence_break():
    assert thesis_excerpt("Services growth") == "Services growth"


# --- I1: real thesis strings that used to dangle mid-sentence ---------------
# These are the exact (YAML-folded) thesis strings from the two real
# strategies the reviewer reproduced the bug against -- a bare `\n` from the
# YAML block scalar's ~80-char hard-wrap used to be treated as a sentence
# boundary, truncating well before any real punctuation.

def test_thesis_excerpt_mu_swing_renders_the_whole_first_sentence_not_a_dangle():
    text = (
        "Micron (MU) is a high-volatility AI memory play (HBM demand supercycle) "
        "with extreme\nprice swings driven by cyclical memory dynamics and shifting "
        "AI sentiment.\n90-day range shows ~70% swing ($737.88 - $1254.81), with "
        "-41% drawdown in one month\nfollowed by sharp rebounds. User wants to use "
        "MU (and similar names like SK Hynix)\nas a SWING/SATELLITE position, not a "
        "long-term hold: buy fear-driven dips, sell\neuphoria-driven rallies, with "
        "strict risk control given the volatility.\nThis is explicitly NOT a "
        "buy-and-hold thesis - position should be flat most of the time,\nonly "
        "active when clear oversold/overbought signals appear.\nInvalidation: "
        "memory market enters structural oversupply / AI HBM demand collapses.\n"
    )
    result = thesis_excerpt(text)
    # The old bug cut at the first bare '\n', landing on "...with extreme" --
    # a dangling clause with no verb, no ellipsis, and no signal it was cut.
    assert not result.endswith("with extreme")
    assert result.endswith("…")
    # The real first sentence is 159 chars, longer than the 140-char cap, so
    # it's still capped -- but at the 140-char mark, not at the mid-clause
    # dangle a bare '\n' used to produce.
    assert result == (
        "Micron (MU) is a high-volatility AI memory play (HBM demand supercycle) "
        "with extreme\nprice swings driven by cyclical memory dynamics and shi…"
    )


def test_thesis_excerpt_tsm_longhold_renders_the_whole_first_sentence_not_a_dangle():
    text = (
        "TSM is the dominant AI chip foundry with strong, stable fundamentals "
        "compared to\nvolatile memory/GPU peers. Revenue growth accelerating "
        "(H1 2026 +35.6% YoY, June +67.9% YoY).\n2026 EPS growth projected +48% to "
        "$15.80. Advanced node capacity (3nm/5nm) remains tight.\nUser views TSM as "
        "the \"stable long-hold\" anchor within a semiconductor basket,\nplanned "
        "hold duration ~6-12 months, contrasted against high-volatility swing names "
        "(MU, SK Hynix).\nInvalidation: foundry demand collapse, major customer "
        "loss (Apple/Nvidia/AMD), or\ngeopolitical disruption to Taiwan operations.\n"
    )
    result = thesis_excerpt(text)
    # The old bug cut at the first bare '\n', landing on "...compared to" --
    # dangling mid-clause, with no ellipsis.
    assert not result.endswith("compared to")
    assert result.endswith("…")
    # The real first sentence (across the hard-wrap fold) fits under the cap,
    # so it renders whole, plus the ellipsis signaling there's more thesis.
    assert result == (
        "TSM is the dominant AI chip foundry with strong, stable fundamentals "
        "compared to\nvolatile memory/GPU peers.…"
    )


def test_thesis_excerpt_short_abbreviation_does_not_fool_the_sentence_boundary():
    # "U.S." would land the naive first '.' match at position 1 -- well
    # under the 20-char guard -- so the real sentence end further along
    # must be the one that wins.
    text = "U.S. stocks are volatile this quarter given rate hikes and inflation data."
    assert thesis_excerpt(text) == text  # whole text: the real sentence IS the text


def test_thesis_excerpt_caps_at_140_chars_with_ellipsis():
    long_sentence = "A" * 200 + "."
    result = thesis_excerpt(long_sentence)
    assert len(result) == 141  # 140 chars + ellipsis marker
    assert result.endswith("…")


def test_thesis_excerpt_strips_surrounding_whitespace():
    assert thesis_excerpt("  Services growth.  \n") == "Services growth."


# --- horizon_label -----------------------------------------------------------

def test_horizon_label_maps_enum_values_to_display_text():
    assert horizon_label("long") == "Long-term"
    assert horizon_label("medium") == "Medium-term"
    assert horizon_label("swing") == "Swing"


def test_horizon_label_none_is_empty_string():
    assert horizon_label(None) == ""
