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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._shutdown_event: asyncio.Event | None = None

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

    def _run_loop(self, ready: threading.Event, errors: list[BaseException]) -> None:
        """Thread body: owns the event loop for the life of the backend.

        Runs one long-lived coroutine that enters the stdio transport and
        session contexts, publishes the session, signals readiness, then
        blocks on the shutdown event -- so the contexts are entered and
        exited on the same loop iteration as required by the MCP SDK.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def main() -> None:
            shutdown_event = asyncio.Event()
            self._shutdown_event = shutdown_event
            try:
                async with (
                    stdio_client(self._server_params()) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    self._session = session
                    ready.set()
                    await shutdown_event.wait()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the caller thread
                errors.append(exc)
                ready.set()
            finally:
                self._session = None

        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

    def _spawn_locked(self) -> None:
        """Start the server thread. Caller must hold `self._lock`."""
        ready = threading.Event()
        errors: list[BaseException] = []
        thread = threading.Thread(
            target=self._run_loop, args=(ready, errors), daemon=True,
        )
        self._thread = thread
        thread.start()
        if not ready.wait(timeout=_STARTUP_TIMEOUT):
            raise OptionsBackendError("options MCP server startup timed out")
        if errors:
            raise OptionsBackendError(f"options MCP server failed to start: {errors[0]}")

    def _teardown_locked(self) -> None:
        """Best-effort shutdown of a possibly-broken session. Caller must
        hold `self._lock`."""
        loop = self._loop
        shutdown_event = self._shutdown_event
        if loop is not None and shutdown_event is not None:
            try:
                loop.call_soon_threadsafe(shutdown_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=_CALL_TIMEOUT)
        self._session = None
        self._loop = None
        self._shutdown_event = None
        self._thread = None

    def stop(self) -> None:
        """Idempotent shutdown: safe to call whether or not the backend
        was ever started."""
        with self._lock:
            self._teardown_locked()

    # -- call plumbing ------------------------------------------------------

    def _invoke_locked(self, tool: str, args: dict[str, Any]) -> str:
        """Run one `call_tool` on the session's loop. Caller must hold
        `self._lock` and have a live `self._session`/`self._loop`."""
        assert self._session is not None and self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, args), self._loop,
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
            if self._session is None:
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
