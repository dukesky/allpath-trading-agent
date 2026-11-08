# R1 gate red-team harness

An offline, deterministic runner that feeds synthetic attacks through Allpath's real code paths and records which layer catches each one. It was built for the IEEE TDSC submission (plan: `allpath-paper/notes/tdsc-plan-2026-11.md`, R1).

```bash
env -u OPENAI_API_KEY uv run python -m research.redteam --out research/redteam/results/$(date +%Y%m%d)-$(git rev-parse --short HEAD)
env -u OPENAI_API_KEY uv run python -m research.redteam --out /tmp/rt --only S10,X3
# official post-fix run, with the before/after table against the baseline:
env -u OPENAI_API_KEY uv run python -m research.redteam --out research/redteam/results/20261106-4e0752a \
  --baseline research/redteam/results/redteam-results.csv --baseline-label baseline-8a5ff4e --label 4e0752a
env -u OPENAI_API_KEY uv run pytest -q tests/test_redteam_harness.py
```

Output, in the `--out` folder: `redteam-results.md` (a per-threat summary plus the full table) and `redteam-results.csv` (one row per case, with `threat`, `goal`, `regime`, `residual` and `fixed_by` columns); with `--baseline`, also `before-after.md`. Re-running gives byte-identical files; a test checks this.

`--out` is required, so no run can overwrite another:
- `results/redteam-results.*` is the baseline (pre-fix, 8a5ff4e); leave it as is.
- `results/20261106-c5e833f/` is the post-fix run for F1, F2, F3 and F6.
- `results/20261106-4e0752a/` adds F7 and is the current run. It has `before-after.md` against the baseline and `before-after-vs-c5e833f.md` against the previous run.

## Files

| Path | What |
|---|---|
| `ATTACKS.md` | The attack list with rationale and the paper's threat (T1–T7) / goal (G1–G6) tags |
| `corpus.yaml` | The attack corpus: per case, metadata, the pre-run prediction, and the adversarial payload |
| `env.py` | `RedteamEnv`: the production assembly (`app.build_components`) on a temp dir, with fakes only at the boundary |
| `cases/` | One runner per case, grouped S/O/D/X/M/H/L (and A for the T6 visibility checks) |
| `result.py` | `Result`, plus `src_line` / `raise_site`, which pin each verdict to `file:line` |
| `report.py`, `runner.py`, `__main__.py` | Table output and the CLI |
| `FINDINGS.md` | Write-up of every not-caught or bounded row |

## What is real and what is fake

Real: the risk gate, executor, journal, review queue and approval tokens, revision applier, regime and breaker, sentinel dispatch, every tool registry as production wires it, the memory guard and store, consolidation, the nightly Reflector, ChatService, and the Telegram poller.

Fake:
- the broker of record: in-memory fills at the fake quote; it accepts any symbol;
- market data: fixed quotes; an unknown ticker raises;
- every LLM tier: scripted replies;
- web search: the attack payloads;
- the notifier: records messages;
- the clock: frozen, with the market open.

`Settings` are built with `_env_file=None` and every credential blanked, so nothing can reach Alpaca, an LLM provider, Telegram or the Air.

Rows marked `LLM layer: scripted` replay a fixed hostile model output. They show what the code does with it, not whether a real model would produce it.

## Adding a case

1. Add the case to `corpus.yaml`, with `expected_layer` / `expected_status` written before running.
2. Register a runner with `@case("ID")` in the matching `cases/` module.
3. Locate verdicts with `raise_site(exc)` or `src_line(path, literal_message)`: a moved line still resolves, and a reworded message fails loudly.

## Before/after runs against another revision

The harness depends only on `allpath_trade`'s public behaviour. Runners judge outcomes by state (DB rows, files on disk, broker submissions), not by success-message wording, and locate code by literal message text, so it runs unchanged against later revisions. For example, after a fix lands on `main`:

```bash
git worktree add --detach /tmp/rt-main main
cp -R research /tmp/rt-main/ && cd /tmp/rt-main
env -u OPENAI_API_KEY uv run python -m research.redteam \
  --out research/redteam/results/$(date +%Y%m%d)-$(git rev-parse --short HEAD)
```

Keep each run in its own dated folder, so the before and after tables both survive. A reworded rejection message makes `src_line` raise a harness error rather than point at the wrong line: update the needle, not the verdict. Rows whose gate a fix adds (S10–S12, X3, O9b, X2, M2, A4b) fall back to a generic "caught" result; tighten their evidence to quote the new gate's message during the after-run.
