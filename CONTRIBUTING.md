# Contributing to AllPath

Thanks for looking. AllPath is a self-hosted LLM stock assistant that proposes, explains, and asks before it trades. Bug reports, small fixes and focused features are all welcome. For anything large, open an issue first so we can agree on the shape before you write code.

## Setup

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/dukesky/allpath-trading-agent.git
cd allpath-trading-agent
uv sync
```

## See it running without keys

```bash
uv run allpath-trade demo
```

Open <http://localhost:8790> and sign in with `demo`. The demo uses sample data, simulated fills and a scripted chat, in its own temp directory. It needs no Alpaca account and no LLM key, and nothing reaches a broker. Use it to check UI changes. Pass `--port` if 8790 is taken and `--dir PATH` to keep the sample data between runs.

## Tests and lint

```bash
env -u OPENAI_API_KEY uv run pytest -q
uv run ruff check path/to/files_you_changed.py
```

The unit suite never touches the network: brokers, data sources and LLM clients are injected fakes. `env -u OPENAI_API_KEY` keeps a key exported in your shell from leaking into settings-dependent tests. Live integration tests are opt-in (`uv run pytest -m integration`, Alpaca paper keys required).

Run both before you open a pull request. Older test files still carry lint warnings, so check the files you touched rather than the whole tree; new and changed files should be clean. Add tests with every bug fix and feature. Code on the money path (risk gate, execution, review queue, strategy apply) needs thorough tests, including the refusal cases.

## Ground rules

- **Paper trading is the default.** `ALPACA_PAPER=true` unless the user changes it. Do not add a code path that places an order without passing the deterministic risk gate, or one that skips approval for a `confirm` strategy.
- **UI chrome stays English.** Labels, buttons, banners, notifications and error messages are English only (tests enforce this with `assert_english_only`). What the user and the agent say to each other can be in any language.
- **Money is `Decimal`**, never `float`.
- **No secrets or personal data in the repo.** No `.env`, API keys, account numbers or real holdings, in code, tests, docs or screenshots. Screenshots and recordings come from `allpath-trade demo` only.
- **Local servers bind to 127.0.0.1** while you develop.

## Branches, commits and pull requests

- Branch names: `type/short-description`, for example `feat/holiday-calendar` or `fix/chart-label-overlap`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/): `feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`, `test: ...`.
- A pull request says what changed and why, how you tested it, and includes a screenshot for UI changes (from the demo). Add a line to `CHANGELOG.md` for anything a user would notice.

## Where to start

Good first areas: a US market holiday calendar, a `docker-compose.yml`, the dashboard chart label overlap, and keeping `README.zh-CN.md` in step with the English README. Larger areas are listed under [Contributing](README.md#contributing) in the README.

## Security

Do not open a public issue with exploit details. Open an issue that says you have a security report and how to reach you, and we will follow up privately.
