from allpath_trade.web.format import horizon_label, thesis_excerpt

# --- thesis_excerpt ---------------------------------------------------------

def test_thesis_excerpt_empty_is_empty_string():
    assert thesis_excerpt("") == ""
    assert thesis_excerpt(None) == ""


def test_thesis_excerpt_takes_only_the_first_sentence():
    text = "Services growth continues. Margin expansion likely next year."
    assert thesis_excerpt(text) == "Services growth continues."


def test_thesis_excerpt_takes_first_line_when_no_terminal_punctuation():
    text = "Bullish on cloud growth\nContinuing expansion into enterprise."
    assert thesis_excerpt(text) == "Bullish on cloud growth"


def test_thesis_excerpt_whole_text_when_no_sentence_break():
    assert thesis_excerpt("Services growth") == "Services growth"


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
