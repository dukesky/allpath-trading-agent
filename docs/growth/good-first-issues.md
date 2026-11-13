# Good first issues: drafts

Five issue drafts, ready to file with the `good first issue` label. They are drafts only: nothing here has been opened on GitHub. File pointers were checked against `main` at `c0f6d03` (2026-11-13).

Each one is small, has a clear finish line, and can be checked with the unit tests or `uv run allpath-trade demo` without any API key.

---

## 1. US market holiday calendar for the market-hours check

**Labels:** `good first issue`, `enhancement`

**Context.** `allpath_trade/market_hours.py` decides whether the US market is open from the weekday and the clock only. Its own docstring says there is no holiday calendar yet, so a market holiday (for example Thanksgiving) reads as an ordinary trading day, and the half-days before some holidays read as full days. The sentinel then evaluates rules against stale prices on a day when nothing can fill.

**What to do.** Add a small, dependency-free holiday calendar for NYSE full-day closures and use it in `is_us_market_open` and `next_us_market_open`. Computing the rules (fixed dates with weekend observance, nth-weekday holidays, Good Friday) is preferred over a hard-coded list of dates that goes stale. Early closes (13:00 ET) are a welcome extra, not required.

**Acceptance criteria.**
- `is_us_market_open` returns `False` all day on each NYSE full-day holiday, for at least 2026 through 2028, including observed dates (a holiday on a Saturday is observed on Friday, on a Sunday on Monday).
- `next_us_market_open` skips holidays the same way it skips weekends.
- New tests in `tests/test_market_hours.py` cover every holiday in one year, one observed-date case, and one ordinary weekday right after a holiday.
- No new runtime dependency. No network access.

**Pointers.** `allpath_trade/market_hours.py` (two functions, about 55 lines), `tests/test_market_hours.py`.

**Note for maintainers.** This changes when the sentinel runs, so merge it after the human-vs-auto study ends on 2026-12-31.

---

## 2. Dashboard equity chart: start and low labels overlap

**Labels:** `good first issue`, `bug`, `ui`

**Context.** The dashboard's equity chart labels the first value, the last value, the high and the low. When the low point is near the start of the series, the low label is drawn on top of the start label and the two become unreadable. It shows up in the demo (`uv run allpath-trade demo`, Dashboard, sign in with `demo`) because the sample curve dips early. First-impression audit item S10 (`docs/growth/2026-11-audit.md`).

**What to do.** In `equity_svg`, detect when the high or low label would collide with the first or last label and resolve it: move the label to the other side of the line, nudge it horizontally, or drop the duplicate. The file already has `_clamp_label_x` for the viewBox edges and a rule that suppresses a hi/lo label that is the same point as an endpoint; this is the near-but-not-equal case.

**Acceptance criteria.**
- With a series whose low is the second or third point, the rendered SVG has no two `<text>` labels whose boxes overlap (a test can compare the x/y of the label elements with a minimum distance).
- The same for a high near the end of the series.
- Existing tests in `tests/test_charts.py` still pass; the demo dashboard chart is readable (attach a before/after screenshot from the demo).

**Pointers.** `allpath_trade/web/charts.py` (`equity_svg`, `_clamp_label_x`), `tests/test_charts.py`, sample curve in `allpath_trade/demo.py` (`DemoBroker.get_equity_history`).

---

## 3. Bring the Chinese README body in line with the English one

**Labels:** `good first issue`, `documentation`, `help wanted`

**Context.** `README.zh-CN.md` has a current top (pitch, demo GIF, quickstart, "Why not X?", Docker) but its body has drifted from `README.md`. The English README has sections the Chinese one lacks or covers only briefly: the strategy system and rule language, the trading loop, options via Alpaca's MCP server, the autonomy ladder table, notifications, and the `.env` configuration table. Some Chinese sections also describe older behaviour (for example hand-editing `.env` before first run, which the setup wizard replaced).

**What to do.** Go through `README.md` section by section and update `README.zh-CN.md` to match in content and order. Translate meaning, not word for word. Keep UI labels, commands, setting names and file names in English, exactly as they appear in the app (for example Pending, Approve, `authorization: confirm`).

**Acceptance criteria.**
- Every `##` section of `README.md` from "Why AllPath" to "License" has a counterpart in `README.zh-CN.md`, in the same order, and the table of contents matches.
- Commands, environment variable names and defaults are identical in both files.
- No statement in the Chinese README contradicts the English one.
- This can be split: a pull request that syncs two or three sections is fine. Say which ones in the description.

**Pointers.** `README.md`, `README.zh-CN.md`. No code changes.

---

## 4. Write the "add a broker adapter" guide

**Labels:** `good first issue`, `documentation`

**Context.** Broker adapters (Interactive Brokers, Tradier, Schwab) are the most requested contribution, and the interface is small: the `Broker` abstract class in `allpath_trade/broker/base.py` has seven methods. There is no document that says what each method must guarantee, so a contributor has to read the Alpaca adapter and guess.

**What to do.** Add `docs/guides/broker-adapter.md`. Read the interface and the three existing implementations, then document, for each method (`get_account`, `get_positions`, `get_order`, `get_orders`, `submit_order`, `cancel_order`, `get_equity_history`): what it returns, which fields of `Account` / `Position` / `Order` must be filled, how errors are reported (`BrokerError`), and anything the rest of the system relies on (money as `Decimal`, the `name` and `is_paper` attributes, notional versus quantity orders). End with a checklist for a new adapter: where it gets constructed (`_build_broker` in `allpath_trade/app.py`), which settings it needs, and which tests to copy.

**Acceptance criteria.**
- The guide covers all seven methods and the `name` / `is_paper` attributes, with a short code skeleton of a new adapter class that imports cleanly.
- It points at the smallest working implementation as the reference (`DemoBroker` in `allpath_trade/demo.py`), then the real one (`allpath_trade/broker/alpaca.py`) and the test file to model new tests on (`tests/test_broker_alpaca.py`).
- It says plainly that a new adapter must default to paper or sandbox mode and that every order still goes through the risk gate and the approval queue: an adapter never bypasses them.
- Linked from the Contributing section of `README.md` and from `CONTRIBUTING.md`.

**Pointers.** `allpath_trade/broker/base.py`, `allpath_trade/broker/alpaca.py`, `allpath_trade/broker/shadow.py`, `allpath_trade/demo.py`, `allpath_trade/app.py` (`_build_broker`). No code changes required.

---

## 5. Clear the remaining ruff findings so CI can lint the whole repo

**Labels:** `good first issue`, `chore`

**Context.** CI (`.github/workflows/ci.yml`) lints the `allpath_trade/` package with the project's full rule set but only checks `tests/` for outright errors, because `uv run ruff check .` still reports about 115 findings: roughly 85 are `FURB157` (`Decimal("100")` where `Decimal(100)` is enough), 17 are `DTZ011` (`date.today()` without a timezone) in tests, and a handful of others. Two are in the package (`UP045` in `allpath_trade/agent/reflection_tools.py`, `PLR1730` in `allpath_trade/risk/breaker.py`) and are ignored in CI by rule code.

**What to do.** Fix the findings in `tests/` so the whole repo passes. Most are mechanical (`uv run ruff check tests --fix` handles the `FURB157` ones); review the diff rather than trusting it blindly, and fix `DTZ011` by passing an explicit timezone. Then simplify CI to a single `uv run ruff check .` step.

**Acceptance criteria.**
- `uv run ruff check .` reports no findings.
- `env -u OPENAI_API_KEY uv run pytest -q` passes with the same test count as before: no test is deleted or skipped to make lint pass.
- `.github/workflows/ci.yml` has one lint step, `uv run ruff check .`, and the `--ignore UP045,PLR1730` flags are gone. `CONTRIBUTING.md` says to run `uv run ruff check .`.
- The two package findings are fixed in a separate commit, because `allpath_trade/risk/breaker.py` is on the money path: that one-line change (`peak = max(peak, equity)`) needs a maintainer's review and should merge after the study ends on 2026-12-31. It is fine to leave it out and keep one `--ignore PLR1730`.

**Pointers.** `uv run ruff check . --statistics` for the current list; `.github/workflows/ci.yml`; `CONTRIBUTING.md` ("Tests and lint").

---

## Spare

If one of the five is taken quickly: the dashboard shows "Sentinel: never ran (daemon not running?)" on a fresh install and in the demo, where it reads like an error (audit item S11). Change the copy to something neutral such as "Sentinel: not run yet" until the first scheduled pass. `allpath_trade/web/routes/dashboard.py` (two return sites), with a test in the dashboard test file.
