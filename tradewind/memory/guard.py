from __future__ import annotations

import re

MAX_ENTRY_CHARS = 500

GUARD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction-override", re.compile(r"ignore\s+(all|any|previous|prior)", re.IGNORECASE)),
    ("role-injection", re.compile(r"\b(system|assistant)\s*:", re.IGNORECASE)),
    ("imperative-pressure", re.compile(r"\byou must\b", re.IGNORECASE)),
    ("unconditional-trade", re.compile(r"\balways\s+(buy|sell)\b", re.IGNORECASE)),
    ("fence-marker", re.compile(r"<\s*/?\s*external-content", re.IGNORECASE)),
    ("urgency-marker", re.compile(r"\bimportant\s*:", re.IGNORECASE)),
    ("url", re.compile(r"https?://", re.IGNORECASE)),
]


class MemoryGuardError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"memory write rejected: {reason}")


def scan_entry(text: str) -> None:
    """Screen a candidate curated-memory entry. Curated memory is executable
    context for a trading agent — a poisoned entry is a delayed exploit."""
    if len(text) > MAX_ENTRY_CHARS:
        raise MemoryGuardError(f"entry too long ({len(text)} > {MAX_ENTRY_CHARS})")
    for name, pattern in GUARD_PATTERNS:
        if pattern.search(text):
            raise MemoryGuardError(f"matched {name} pattern")
