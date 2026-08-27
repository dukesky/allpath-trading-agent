"""Sync `OptionsBackend` facade over Alpaca's official MCP server.

`uvx alpaca-mcp-server` speaks MCP over stdio and only exposes an async
API (`mcp.ClientSession`). Every other broker call in this codebase is
synchronous (see `broker/base.py`, `broker/alpaca.py`), so `McpOptionsBackend`
hides the asyncio session behind a daemon thread that owns one long-lived
event loop: the stdio transport and the `ClientSession` are opened once on
that loop and kept alive for the process's lifetime (the MCP SDK's
`async with` context managers are not safe to enter on one loop iteration
and exit on another, so they live inside a single coroutine that runs for
as long as the backend is up). Sync callers submit work onto that loop with
`asyncio.run_coroutine_threadsafe` and block on the result; a
`threading.Lock` serializes calls so two threads never drive the same
`ClientSession` concurrently.

Each spawn attempt (initial start, or a respawn after a transport failure)
gets its own `_ServerHandle` -- its own thread, event loop, ready/shutdown
events, and session slot. The background thread only ever writes to ITS
`_ServerHandle`'s fields, never directly to `self.*`; `self._current` is
assigned exactly once per successful attempt, by `_spawn_locked` itself
(always under `self._lock`), only after that exact attempt's `ready` has
fired. This is what keeps a slow or hung attempt (e.g. a startup timeout)
from ever overwriting the handle a later, successful attempt publishes: an
abandoned handle simply has nothing left pointing at it once
`_spawn_locked` raises, so it can't corrupt `self._current` no matter how
long it takes to actually unwind in the background.

Probed facts this module hard-codes (see
docs/superpowers/specs/2026-08-27-options-via-mcp-design.md, "Probed
facts", verified live 2026-08-27 against the real server):
- Tool results are JSON text: `{"_alpaca_mcp_security": {...}, "data": {...}}`.
- Tool-level errors come back as plain text starting with
  "Error calling tool" rather than as a protocol-level error.
- `get_option_contracts` returns `data["option_contracts"]`, each with
  `symbol` (OCC), `expiration_date` ("YYYY-MM-DD"), `strike_price`
  (numeric string), `tradable` (bool).
- `get_option_latest_quote` returns `data["quotes"][occ_symbol]["ap"]`
  (ask price, float).
- `place_option_order` takes OCC `symbol`, `side` ("buy"/"sell"), a
  STRING `qty`, and `position_intent`
  ("buy_to_open"/"sell_to_close" for this app's two uses).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

from allpath_trade.config import Settings

# Per-call timeout for anything submitted onto the MCP event loop thread.
_CALL_TIMEOUT = 30.0
# How long to wait for the server subprocess to start and initialize before
# giving up on it.
_STARTUP_TIMEOUT = 30.0


class OptionsBackendError(Exception):
    """Any failure talking to the options MCP server: transport failure
    (after the one respawn attempt), a startup timeout, or a tool-level
    error string from the server itself."""


class OptionPick(BaseModel):
    """One contract selected by `pick_contract`, sized to a budget."""

    occ_symbol: str
    expiry: date
    strike: Decimal
    ask: Decimal          # per-share ask at selection time
    qty: int               # contracts
    est_premium: Decimal   # total dollars = ask * 100 * qty


class OptionsBackend(Protocol):
    def pick_contract(self, underlying: str, right: str, min_dte: int,
                       otm_pct: Decimal, budget: Decimal,
                       spot: Decimal) -> OptionPick | None: ...

    def place_option_order(self, occ_symbol: str, side: str, qty: int,
                            position_intent: str) -> dict: ...

    def stop(self) -> None: ...


class _ServerHandle:
    """State for exactly one spawn attempt of the MCP server subprocess:
    its own thread, its own event loop, its own ready/shutdown events, its
    own session slot. Nothing outside `McpOptionsBackend._run_loop` writes
    to these fields, and `_run_loop` never touches `McpOptionsBackend.*`
    directly -- only its own `_ServerHandle` -- so an attempt that never
    gets published to `self._current` (e.g. because it timed out) cannot
    corrupt whatever attempt becomes current next, no matter how long it
    takes to actually unwind."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.errors: list[BaseException] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.shutdown_event: asyncio.Event | None = None
        self.session: ClientSession | None = None
        self.thread: threading.Thread | None = None


class McpOptionsBackend:
    """Lazy-starting `OptionsBackend` backed by `uvx alpaca-mcp-server`.

    Nothing is spawned until the first `pick_contract` / `place_option_order`
    call. `stop()` is idempotent and safe to call even if the backend was
    never started.
    """

    name = "mcp"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.Lock()
        self._current: _ServerHandle | None = None

    # -- subprocess / session lifecycle -----------------------------------

    def _server_params(self) -> StdioServerParameters:
        # Never log this dict -- it carries live trading credentials.
        env = {
            **os.environ,
            "ALPACA_API_KEY": self._settings.alpaca_api_key,
            "ALPACA_SECRET_KEY": self._settings.alpaca_secret_key,
            "ALPACA_PAPER_TRADE": "true",
        }
        return StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env=env)

    def _run_loop(self, handle: _ServerHandle) -> None:
        """Thread body: owns the event loop for the life of this attempt.

        Runs one long-lived coroutine that enters the stdio transport and
        session contexts, publishes the session onto `handle` (never onto
        `self`), signals readiness, then blocks on the shutdown event --
        so the contexts are entered and exited on the same loop iteration
        as required by the MCP SDK.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        handle.loop = loop

        async def main() -> None:
            shutdown_event = asyncio.Event()
            handle.shutdown_event = shutdown_event
            try:
                async with (
                    stdio_client(self._server_params()) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    handle.session = session
                    handle.ready.set()
                    await shutdown_event.wait()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the caller thread
                handle.errors.append(exc)
                handle.ready.set()
            finally:
                handle.session = None

        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

    def _abandon(self, handle: _ServerHandle) -> None:
        """Best-effort teardown of one `_ServerHandle`'s thread/subprocess.

        Used both for normal teardown of the current handle and for a
        handle that must never become current (startup timed out). Signals
        that handle's own shutdown event via its own loop, then joins its
        own thread -- this only ever touches `handle`'s fields, so it is
        safe to call on an attempt that was never published to
        `self._current`.
        """
        if handle.loop is not None and handle.shutdown_event is not None:
            shutdown_event = handle.shutdown_event
            try:
                handle.loop.call_soon_threadsafe(shutdown_event.set)
            except RuntimeError:
                pass
        if handle.thread is not None:
            handle.thread.join(timeout=_CALL_TIMEOUT)

    def _spawn_locked(self) -> None:
        """Start one spawn attempt. Caller must hold `self._lock`.

        `self._current` is assigned exactly once here, only after THIS
        attempt's `ready` has fired successfully -- a timed-out or failed
        attempt is torn down via `_abandon` and never published, so it can
        never overwrite a handle a later attempt sets as current.
        """
        handle = _ServerHandle()
        thread = threading.Thread(target=self._run_loop, args=(handle,), daemon=True)
        handle.thread = thread
        thread.start()

        if not handle.ready.wait(timeout=_STARTUP_TIMEOUT):
            self._abandon(handle)
            raise OptionsBackendError("options MCP server startup timed out")
        if handle.errors:
            # main() already saw the failure and set `ready` itself, so
            # its coroutine is already exiting -- just bound the join.
            if handle.thread is not None:
                handle.thread.join(timeout=_CALL_TIMEOUT)
            raise OptionsBackendError(f"options MCP server failed to start: {handle.errors[0]}")

        self._current = handle

    def _teardown_locked(self) -> None:
        """Best-effort shutdown of the current session, if any. Caller
        must hold `self._lock`."""
        handle, self._current = self._current, None
        if handle is not None:
            self._abandon(handle)

    def stop(self) -> None:
        """Idempotent shutdown: safe to call whether or not the backend
        was ever started."""
        with self._lock:
            self._teardown_locked()

    # -- call plumbing ------------------------------------------------------

    def _invoke_locked(self, tool: str, args: dict[str, Any]) -> str:
        """Run one `call_tool` on the current handle's loop. Caller must
        hold `self._lock` and have a live `self._current`."""
        handle = self._current
        assert handle is not None and handle.session is not None and handle.loop is not None
        future = asyncio.run_coroutine_threadsafe(
            handle.session.call_tool(tool, args), handle.loop,
        )
        result = future.result(timeout=_CALL_TIMEOUT)
        return result.content[0].text

    def _call(self, tool: str, args: dict[str, Any]) -> dict:
        """Invoke an MCP tool and return its parsed `data` envelope.

        On any failure reaching or using the session, tears the session
        down and makes one respawn attempt before raising
        `OptionsBackendError`.
        """
        with self._lock:
            if self._current is None:
                self._spawn_locked()
            try:
                text = self._invoke_locked(tool, args)
            except Exception:  # noqa: BLE001 - any transport failure triggers respawn
                self._teardown_locked()
                self._spawn_locked()
                try:
                    text = self._invoke_locked(tool, args)
                except Exception as second_exc:
                    raise OptionsBackendError(
                        f"options MCP call '{tool}' failed after respawn: {second_exc}"
                    ) from second_exc
        return self._parse_result(text)

    @staticmethod
    def _parse_result(text: str) -> dict:
        """Parse one tool result's text into its `data` envelope.

        A tool-level error comes back as plain text starting with
        "Error calling tool" (not a protocol error) -- raise
        `OptionsBackendError` for that; otherwise the text is strict JSON
        `{"_alpaca_mcp_security": {...}, "data": {...}}` and only `data`
        matters to callers.
        """
        if text.startswith("Error calling tool"):
            raise OptionsBackendError(text)
        return json.loads(text)["data"]

    # -- OptionsBackend surface ---------------------------------------------

    def pick_contract(self, underlying: str, right: str, min_dte: int,
                       otm_pct: Decimal, budget: Decimal,
                       spot: Decimal) -> OptionPick | None:
        right = right.lower()
        expiry_bound = datetime.now(UTC).date() + timedelta(days=min_dte)
        data = self._call("get_option_contracts", {
            "underlying_symbols": underlying,
            "type": right,
            "expiration_date_gte": expiry_bound.isoformat(),
            "limit": 300,
        })
        contracts = [c for c in data.get("option_contracts", []) if c.get("tradable")]
        if not contracts:
            return None

        # Nearest expiry on/after the bound. "expiration_date" is
        # "YYYY-MM-DD", which sorts lexicographically the same as
        # chronologically.
        nearest_expiry = min(c["expiration_date"] for c in contracts)
        same_expiry = [c for c in contracts if c["expiration_date"] == nearest_expiry]

        if right == "call":
            target_strike = spot * (Decimal(1) + otm_pct)
        else:
            target_strike = spot * (Decimal(1) - otm_pct)
        best = min(
            same_expiry,
            key=lambda c: abs(Decimal(str(c["strike_price"])) - target_strike),
        )
        occ_symbol = best["symbol"]

        quote_data = self._call("get_option_latest_quote", {"symbols": occ_symbol})
        quote = quote_data.get("quotes", {}).get(occ_symbol)
        if not quote:
            return None
        ask = Decimal(str(quote.get("ap", 0)))
        if ask <= 0:
            return None

        qty = int(budget // (ask * Decimal(100)))
        if qty < 1:
            return None

        return OptionPick(
            occ_symbol=occ_symbol,
            expiry=date.fromisoformat(best["expiration_date"]),
            strike=Decimal(str(best["strike_price"])),
            ask=ask,
            qty=qty,
            est_premium=ask * Decimal(100) * qty,
        )

    def place_option_order(self, occ_symbol: str, side: str, qty: int,
                            position_intent: str) -> dict:
        return self._call("place_option_order", {
            "symbol": occ_symbol,
            "side": side,
            "qty": str(qty),
            "type": "market",
            "time_in_force": "day",
            "position_intent": position_intent,
        })
