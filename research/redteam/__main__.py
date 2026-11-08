"""`uv run python -m research.redteam --out DIR [--only S1,O3] [--baseline CSV --label REV]`"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from research.redteam import corpus, report, runner

RESULTS = Path(__file__).with_name("results")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.redteam")
    # Required: each run gets its own dated folder, so no run can overwrite
    # the baseline (results/redteam-results.*, the pre-fix run at 8a5ff4e).
    parser.add_argument("--out", type=Path, required=True,
                        help="output folder, e.g. research/redteam/results/20261106-c5e833f")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="earlier redteam-results.csv; writes before-after.md")
    parser.add_argument("--baseline-label", default="baseline",
                        help="name of the baseline run in before-after.md")
    parser.add_argument("--label", default="this run",
                        help="name of this run in before-after.md (e.g. c5e833f)")
    args = parser.parse_args(argv)
    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    with tempfile.TemporaryDirectory(prefix="redteam-") as tmp:
        results = runner.run_all(Path(tmp), only=only)
    meta = corpus.by_id()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "redteam-results.csv").write_text(report.to_csv(results, meta))
    (args.out / "redteam-results.md").write_text(report.to_markdown(results, meta))
    if args.baseline is not None:
        import csv

        baseline = list(csv.DictReader(args.baseline.open()))
        (args.out / "before-after.md").write_text(report.before_after(
            baseline, results, meta, before_label=args.baseline_label,
            after_label=args.label))
    for r in results:
        print(f"{r.id:5} {r.status:12} {r.layer:10} {r.evidence[:90]}")
    errors = [r for r in results if r.status == "error"]
    print(f"\n{len(results)} cases, {len(errors)} harness errors -> {args.out}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
