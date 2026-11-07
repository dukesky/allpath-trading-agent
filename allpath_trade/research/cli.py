"""`allpath-research`: read-only exports for the paper.

    allpath-research regime-metrics --db allpath-trade.db --from 2026-08-11 \\
        --to 2026-10-02 --out research/tdsc/out

Unlike `allpath-trade`, this never loads `.env`, never migrates files and
never opens the DB for writing, so it is safe to point at the live
production DB on the Air or at a copy."""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

from allpath_trade.research.db import ET, open_readonly


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _date(s: str) -> str:
    return date.fromisoformat(s).isoformat()


def cmd_regime_metrics(args: argparse.Namespace) -> int:
    from allpath_trade.research import regime_metrics as rm

    conn = open_readonly(args.db)
    out = Path(args.out)
    daily = rm.daily_rows(conn, args.start, args.end)
    write_csv(out / "regime_metrics_daily.csv", rm.DAILY_COLUMNS, daily)
    write_csv(out / "revisions.csv", rm.REVISION_COLUMNS,
              rm.revision_rows(conn, args.start, args.end))
    write_csv(out / "orders.csv", rm.ORDER_COLUMNS, rm.order_rows(conn, args.start, args.end))
    usage, failures = rm.llm_rows(conn, args.start, args.end)
    write_csv(out / "llm_daily.csv", rm.LLM_COLUMNS, usage)
    write_csv(out / "llm_failures_daily.csv", rm.LLM_FAILURE_COLUMNS, failures)
    (out / "summary.md").write_text(rm.summary_markdown(
        daily, db_label=Path(args.db).name, start=args.start, end=args.end))
    conn.close()
    print(f"wrote {len(daily)} account-days to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="allpath-research", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    rm = sub.add_parser("regime-metrics", help="TDSC R4: per account per day CSV")
    rm.add_argument("--db", required=True, help="SQLite file (opened read-only)")
    rm.add_argument("--from", dest="start", type=_date, default="2026-08-11",
                    help="first ET date (default 2026-08-11)")
    rm.add_argument("--to", dest="end", type=_date, default=datetime.now(ET).date().isoformat(),
                    help="last ET date (default today)")
    rm.add_argument("--out", required=True, help="output directory")
    rm.set_defaults(func=cmd_regime_metrics)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: no such database: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
