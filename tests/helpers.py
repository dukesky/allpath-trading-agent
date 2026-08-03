"""Shared test-only helpers. Not part of the shipped package."""

from __future__ import annotations

# F4: U+4E00-U+9FFF (CJK Unified Ideographs) alone would catch realistic
# Chinese prose but let a stray fullwidth comma or CJK-style bracket in an
# otherwise-translated line pass. Also covers CJK Symbols and Punctuation
# (U+3000-U+303F: `、。「」`), Halfwidth and Fullwidth Forms (U+FF00-U+FFEF:
# `，！？（）`), and CJK Unified Ideographs Extension A (U+3400-U+4DBF) --
# the same three gaps a leftover-Chinese-fragment bug (wave 2's Finding C4)
# could otherwise slip through as.
_CJK_RANGES = [
    ("一", "鿿"),  # U+4E00-U+9FFF: CJK Unified Ideographs
    ("㐀", "䶿"),  # U+3400-U+4DBF: CJK Unified Ideographs Extension A
    ("　", "〿"),  # CJK Symbols and Punctuation
    ("＀", "￯"),  # Halfwidth and Fullwidth Forms
]


def assert_english_only(text: str) -> None:
    """All user-facing text must be English -- the two READMEs are the only
    place Chinese belongs (docs/TODO.md is a Chinese dev doc and is exempt
    for the same reason the READMEs are: it isn't user-facing).

    Centralizes the check that used to be copy-pasted ad hoc into
    test_web_dashboard.py and test_notify_events.py, so every rendered
    surface -- all six pages, the login page, CLI output -- enforces the
    same invariant the same way instead of two of nine surfaces enforcing it
    and the rest trusting it by omission.
    """
    offending = [ch for ch in text if any(lo <= ch <= hi for lo, hi in _CJK_RANGES)]
    assert not offending, f"unexpected CJK character(s) in user-facing text: {offending!r}"
