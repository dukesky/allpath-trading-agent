"""Runs every corpus case in its own fresh environment."""

from __future__ import annotations

import contextlib
import io
import traceback
from pathlib import Path

from research.redteam import corpus
from research.redteam.cases import RUNNERS
from research.redteam.env import DEFAULT_REGIME
from research.redteam.result import Result

__all__ = ["RUNNERS", "run_all", "run_case"]


def run_case(case: dict, root: Path) -> Result:
    fn = RUNNERS[case["id"]]
    root.mkdir(parents=True, exist_ok=True)
    # Production code prints diagnostics to stderr on some degraded paths
    # (a failed notification, a fill refresh); keep the run output to the
    # table only.
    token = DEFAULT_REGIME.set(case.get("regime"))
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            result = fn(root, case.get("payload") or {})
    except Exception as exc:  # noqa: BLE001 — a broken row is reported, not fatal
        tb = traceback.extract_tb(exc.__traceback__)[-1]
        return Result(id=case["id"], status="error", layer="none",
                      evidence=f"harness error: {type(exc).__name__}: {exc}",
                      location="n/a", notes=f"at {Path(tb.filename).name}:{tb.lineno}")
    finally:
        DEFAULT_REGIME.reset(token)
    result.id = case["id"]
    return result


def run_all(root: Path, only: list[str] | None = None) -> list[Result]:
    cases = corpus.load()
    if only is not None:
        cases = [c for c in cases if c["id"] in only]
    return [run_case(c, Path(root) / c["id"]) for c in cases]
