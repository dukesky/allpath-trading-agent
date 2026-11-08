"""One harness result and the helpers that pin it to a code location."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "allpath_trade"


class HarnessError(Exception):
    """The harness itself is broken (a moved message, a fixture bug). Never a
    verdict about the system under test."""


@dataclass
class Result:
    id: str
    # caught | bounded | neutralised | not caught | error
    status: str
    # code | human | self-review | risk caps | none  (who stopped it)
    layer: str
    # The exact rejection message, or the observed behaviour.
    evidence: str
    # file:line of the deciding code ("; "-joined when two gates act).
    location: str
    notes: str = ""


def src_line(relpath: str, needle: str) -> str:
    """`relpath:N` for the first line of `relpath` containing `needle`.

    Used where a gate decides without raising (a RiskDecision reason, a
    tool's error string): the needle is the message's literal text, so a
    moved line keeps resolving, and a reworded message fails loudly here
    instead of silently pointing at the wrong line."""
    path = REPO / relpath
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if needle in line:
            return f"{relpath}:{n}"
    raise HarnessError(f"{needle!r} not found in {relpath}")


def raise_site(exc: BaseException) -> str:
    """`file:line` of the innermost allpath_trade frame in `exc`'s traceback,
    i.e. the gate that raised it."""
    frames = traceback.extract_tb(exc.__traceback__)
    for frame in reversed(frames):
        path = Path(frame.filename).resolve()
        if path.is_relative_to(PACKAGE):
            return f"{path.relative_to(REPO)}:{frame.lineno}"
    raise HarnessError(f"no allpath_trade frame in traceback of {exc!r}")
