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
    write_csv(out / "order_threads.csv", rm.ORDER_THREAD_COLUMNS,
              rm.order_thread_rows(conn, args.start, args.end))
    write_csv(out / "card_briefs.csv", rm.CARD_BRIEF_COLUMNS,
              rm.card_brief_rows(conn, args.start, args.end))
    write_csv(out / "card_brief_drops.csv", rm.CARD_BRIEF_DROP_COLUMNS,
              rm.card_brief_drop_rows(conn, args.start, args.end))
    holds = rm.hold_rows(conn, args.start, args.end)
    write_csv(out / "holds.csv", rm.HOLD_COLUMNS, holds)
    (out / "summary.md").write_text(rm.summary_markdown(
        daily, db_label=Path(args.db).name, start=args.start, end=args.end,
        holds=holds))
    conn.close()
    print(f"wrote {len(daily)} account-days to {out}")
    return 0


def cmd_rule_outcomes(args: argparse.Namespace) -> int:
    from allpath_trade.research import rule_outcomes as ro

    conn = open_readonly(args.db)
    out = Path(args.out)
    sources = None if args.sources == "all" else tuple(args.sources.split(","))
    rows = ro.rule_outcome_rows(conn, sources=sources, start=args.start, end=args.end)
    write_csv(out / "rule_outcomes.csv", ro.COLUMNS, rows)
    (out / "rule_outcomes_summary.md").write_text(ro.summary_markdown(
        rows, db_label=Path(args.db).name, sources=args.sources))
    conn.close()
    print(f"wrote {len(rows)} rule-outcome rows to {out}")
    return 0


def cmd_replay_rules(args: argparse.Namespace) -> int:
    from allpath_trade.research import replay as rp
    from allpath_trade.research.bars import BarCache, yfinance_fetch

    conn = open_readonly(args.db)
    out = Path(args.out)
    cache = BarCache(args.bars or out / "bars",
                     fetch=None if args.offline else yfinance_fetch)
    sources = None if args.sources == "all" else tuple(args.sources.split(","))
    rows = rp.replay_rows(conn, cache, asof=args.asof, sources=sources,
                          start=args.start, end=args.end)
    write_csv(out / "rule_replay.csv", rp.COLUMNS, rows)
    (out / "rule_replay_summary.md").write_text(rp.summary_markdown(
        rows, db_label=Path(args.db).name, asof=args.asof))
    conn.close()
    print(f"wrote {len(rows)} replay rows to {out} (bars cache: {cache.directory})")
    return 0


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", required=True, help="SQLite file (opened read-only)")
    p.add_argument("--from", dest="start", type=_date, default="2026-08-11",
                   help="first ET date (default 2026-08-11)")
    p.add_argument("--to", dest="end", type=_date,
                   default=datetime.now(ET).date().isoformat(),
                   help="last ET date (default today)")
    p.add_argument("--out", required=True, help="output directory")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="allpath-research", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    rm = sub.add_parser("regime-metrics", help="TDSC R4: per account per day CSV")
    _common(rm)
    rm.set_defaults(func=cmd_regime_metrics)
    ro = sub.add_parser("rule-outcomes", help="TDSC R3: did revised rules fire")
    _common(ro)
    ro.add_argument("--sources", default="reflection",
                    help="revision sources, comma-separated, or 'all' (default reflection)")
    ro.set_defaults(func=cmd_rule_outcomes)
    rp = sub.add_parser("replay-rules",
                        help="TDSC R2: vetoed and removed rules replayed on daily bars")
    _common(rp)
    rp.add_argument("--sources", default="all",
                    help="revision sources, comma-separated, or 'all' (default all)")
    rp.add_argument("--asof", type=_date, default=datetime.now(ET).date().isoformat(),
                    help="last bar date used (default today); fix it for reproducible reruns")
    rp.add_argument("--bars", help="bar cache directory (default <out>/bars)")
    rp.add_argument("--offline", action="store_true",
                    help="never fetch; fail rows whose bars are not cached")
    rp.set_defaults(func=cmd_replay_rules)
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
