"""Shared test-only helpers. Not part of the shipped package."""

from __future__ import annotations

_CJK_LOW, _CJK_HIGH = "一", "鿿"


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
    offending = [ch for ch in text if _CJK_LOW <= ch <= _CJK_HIGH]
    assert not offending, f"unexpected CJK character(s) in user-facing text: {offending!r}"
