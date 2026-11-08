"""One runner per corpus case, registered by id. Group modules are imported
at the bottom so their `@case` decorators populate RUNNERS."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from research.redteam.result import Result

Runner = Callable[[Path, dict], Result]
RUNNERS: dict[str, Runner] = {}


def case(case_id: str) -> Callable[[Runner], Runner]:
    def register(fn: Runner) -> Runner:
        if case_id in RUNNERS:
            raise ValueError(f"duplicate runner for {case_id}")
        RUNNERS[case_id] = fn
        return fn
    return register


from research.redteam.cases import (  # noqa: F401
    dispatch,
    external,
    human,
    memory,
    orders,
    oversight,
    strategy,
    tools,
)
