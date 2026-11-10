"""Keyless chat for `allpath-trade demo`: a small scripted stand-in for the LLM.

A first-time visitor has no LLM key, and the product's promise is "talk to
it". With no key configured, the demo's chat runs on `DemoLLM`: a handful of
recognised questions drive the REAL tool loop (`AgentSession`, the tool
registry, the review queue) against the sandboxed sample data, so a strategy
draft asked for in chat really lands on the Pending page. Everything else
gets one honest sentence saying this is a scripted demo and how to add a key.

Reachable only through `cli.prepare_demo`, which hands `build_demo_llm` to
`create_app`. `serve` never passes it, so a real install with no key keeps
its "Chat needs an LLM key" notice. With a key present `build_demo_llm`
returns the real client and nothing here runs.
"""

from __future__ import annotations

import re
import sqlite3
from decimal import Decimal, InvalidOperation

from allpath_trade.llm.base import LLMClient, LLMResponse, ToolCall, ToolSpec

DEMO_MODEL = "demo-scripted"
LABEL = "*Scripted demo answer (sample data, no LLM).*"

PROMPT_WHY = "Why did you propose this NVDA order?"
PROMPT_RISK = "What's my riskiest position?"
PROMPT_DRAFT = "Draft a pullback strategy for AMZN."
SUGGESTED_PROMPTS = (PROMPT_WHY, PROMPT_RISK, PROMPT_DRAFT)

DEFAULT_DRAFT_TICKER = "AMZN"
PULLBACK = Decimal("0.95")  # entry trigger: 5% under the sample price
STARTER_NOTIONAL = 2500

UNSCRIPTED_REPLY = (
    "This is the keyless demo: a small scripted stand-in answers a few sample "
    "questions, and that was not one of them. Try one of the suggested "
    "prompts below.\n\n"
    "For real answers, add an LLM key: open **Settings** and paste an "
    "OpenRouter, Anthropic or OpenAI key (it is saved only in this demo's "
    "own folder), or stop the demo, `export OPENROUTER_API_KEY=...` and "
    "start it again. The chat then runs on a real model against the same "
    "sample data.")

SUMMARY_REPLY = "Earlier keyless-demo conversation (scripted answers on sample data)."

SAMPLE_CHAT = (
    {"role": "user", "content": "What happened in my account overnight?",
     "source": "demo"},
    {"role": "assistant", "content": (
        f"{LABEL}\n\n"
        "Two things are waiting for you on **Pending**:\n\n"
        "- **NVDA buy, $2,000.** The `add-on-pullback` rule fired (price under "
        "215, weight under 12%). The strategy is `confirm`, so the order "
        "waits for your click.\n"
        "- **AAPL stop revision.** Last night's reflection wants to raise the "
        "stop from 225 to 245, because the position is 11% above entry.\n\n"
        "Nothing trades and no strategy file changes until you approve. "
        "Ask me why I proposed either one.")},
)

_WORDS_NOT_TICKERS = frozenset({"A", "I", "AN", "THE", "FOR", "ON", "OF", "TO",
                                "ME", "MY", "DRAFT", "BUY", "SELL", "LLM", "AI"})
_ORDER_ROW = re.compile(
    r"^#(?P<id>\d+) (?P<sid>[\w-]+)/(?P<rule>[\w-]+) \[(?P<type>\w+)\] "
    r"(?P<condition>.+) -> (?P<action>.+)$", re.MULTILINE)
_POSITION_ROW = re.compile(
    r"^\s+(?P<ticker>[A-Z.]+): qty=(?P<qty>\S+) avg=(?P<avg>\S+) "
    r"value=(?P<value>\S+) pl=(?P<pl>\S+)$", re.MULTILINE)
_EQUITY = re.compile(r"equity=(?P<equity>\S+) cash=(?P<cash>\S+)")
_QUOTE = re.compile(r"^(?P<ticker>[A-Z.]+): (?P<price>[0-9.]+) ")
_QUEUED = re.compile(r"queued for your approval as #(\d+)")


def _text(content) -> str:
    """Plain text of a message's content (a str, or the list shape a turn
    with images uses)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _money(value: Decimal) -> str:
    return f"${value:,.0f}"


def _pct(value: Decimal) -> str:
    return f"{value * 100:.1f}%"


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _portfolio(text: str) -> tuple[Decimal | None, Decimal | None, list[dict]]:
    head = _EQUITY.search(text)
    equity = _dec(head["equity"]) if head else None
    cash = _dec(head["cash"]) if head else None
    rows = []
    for m in _POSITION_ROW.finditer(text):
        value, pl = _dec(m["value"]), _dec(m["pl"])
        if value is not None and pl is not None:
            rows.append({"ticker": m["ticker"], "value": value, "pl": pl})
    return equity, cash, rows


def _draft_ticker(question: str) -> str:
    for word in re.findall(r"\b[A-Z]{2,5}\b", question):
        if word not in _WORDS_NOT_TICKERS:
            return word
    return DEFAULT_DRAFT_TICKER


def _draft_yaml(ticker: str, trigger: Decimal) -> str:
    return (
        f"id: {ticker.lower()}-pullback-entry\n"
        f"name: {ticker} - starter position on a pullback\n"
        "status: draft\n"
        "version: 1\n"
        "authorization: confirm\n"
        "thesis: >\n"
        f"  Sample draft from the keyless demo. Do not chase {ticker} at the\n"
        f"  current price. Open a starter position only after a 5% pullback,\n"
        "  and ask before every order.\n"
        "position:\n"
        f"  ticker: {ticker}\n"
        "  target_weight: '0.05'\n"
        "rules:\n"
        "- id: pullback-entry\n"
        "  type: soft\n"
        f"  condition: position_weight == 0 and price < {trigger}\n"
        f"  action: buy ${STARTER_NOTIONAL}\n"
        "  state: armed\n")


class DemoLLM(LLMClient):
    """Scripted `LLMClient`. Stateless: each `complete` call reads the
    request (the last user message and the tool results after it) and
    returns the next step of that question's script, so the ordinary
    `AgentSession` loop drives it like any model."""

    model = DEMO_MODEL
    suggested_prompts = SUGGESTED_PROMPTS

    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        if not tools:
            # The Compactor's summarisation call: no tools, no question.
            return LLMResponse(text=SUMMARY_REPLY)
        last_user = max((i for i, m in enumerate(messages)
                         if m.get("role") == "user"), default=None)
        if last_user is None:
            return LLMResponse(text=UNSCRIPTED_REPLY)
        question = _text(messages[last_user].get("content"))
        results = self._tool_results(messages[last_user:])
        available = {t.name for t in tools}
        script = self._script_for(question)
        if script is None:
            return LLMResponse(text=UNSCRIPTED_REPLY)
        response = script(question, results, available)
        for call in response.tool_calls:  # unique across the conversation
            call.id = f"demo-{len(messages)}-{call.name}"
        return response

    # -- routing --

    def _script_for(self, question: str):
        q = question.lower()
        if "pullback" in q or ("draft" in q and "strategy" in q):
            return self._draft
        if "riskiest" in q or "biggest risk" in q:
            return self._risk
        if "nvda" in q and ("why" in q or "order" in q or "propos" in q):
            return self._why
        return None

    @staticmethod
    def _tool_results(tail: list[dict]) -> dict[str, str]:
        """Tool name -> result text, for the calls made since the question."""
        names: dict[str, str] = {}
        results: dict[str, str] = {}
        for m in tail:
            for call in m.get("tool_calls") or []:
                names[call["id"]] = call["name"]
            if m.get("role") == "tool" and m.get("tool_call_id") in names:
                results[names[m["tool_call_id"]]] = _text(m.get("content"))
        return results

    @staticmethod
    def _call(*calls: tuple[str, dict]) -> LLMResponse:
        return LLMResponse(
            tool_calls=[ToolCall(id=name, name=name, arguments=args)
                        for name, args in calls],
            stop_reason="tool_use")

    # -- scripts --

    def _why(self, _question, results, available) -> LLMResponse:
        wanted = [n for n in ("list_pending_reviews", "get_quote", "get_portfolio")
                  if n in available]
        if any(n not in results for n in wanted):
            args = {"get_quote": {"ticker": "NVDA"}}
            return self._call(*[(n, args.get(n, {})) for n in wanted
                                if n not in results])
        row = next((m for m in _ORDER_ROW.finditer(results.get("list_pending_reviews", ""))
                    if "nvda" in m["sid"].lower()), None)
        if row is None:
            return LLMResponse(text=(
                f"{LABEL}\n\nThere is no pending NVDA order right now. If you "
                "already approved or rejected it, the outcome is on the "
                "Dashboard under recent trades."))
        quote = _QUOTE.match(results.get("get_quote", ""))
        equity, _cash, rows = _portfolio(results.get("get_portfolio", ""))
        nvda = next((r for r in rows if r["ticker"] == "NVDA"), None)
        reasons = [
            f"**The rule fired.** `{row['rule']}` in strategy `{row['sid']}` "
            f"says `{row['condition']}`"
            + (f", and the sample price is {quote['price']}." if quote else "."),
        ]
        if equity and nvda:
            reasons.append(
                f"**There is room.** NVDA is {_pct(nvda['value'] / equity)} of "
                f"the account ({_money(nvda['value'])} of {_money(equity)}), so "
                f"`{row['action']}` keeps it under the 12% target.")
        reasons.append(
            f"**It is a {row['type']} rule on a `confirm` strategy.** Code "
            "watched the condition; nothing is sent until you press Approve, "
            "and the risk gate checks the order again at that moment.")
        body = "\n".join(f"{i}. {r}" for i, r in enumerate(reasons, start=1))
        return LLMResponse(text=(
            f"{LABEL}\n\nOrder #{row['id']} ({row['action']}) is waiting on "
            f"**Pending**. Why I proposed it:\n\n{body}\n\n"
            "Reject it there if the pullback looks like more than noise to you."))

    def _risk(self, _question, results, available) -> LLMResponse:
        if "get_portfolio" in available and "get_portfolio" not in results:
            return self._call(("get_portfolio", {}))
        equity, cash, rows = _portfolio(results.get("get_portfolio", ""))
        if not equity or not rows:
            return LLMResponse(text=(
                f"{LABEL}\n\nThis account has no open positions, so there is "
                "nothing to rank."))
        rows.sort(key=lambda r: r["value"], reverse=True)
        top = rows[0]
        lines = []
        for r in rows:
            cost = r["value"] - r["pl"]
            gain = f", {_pct(r['pl'] / cost)} on cost" if cost > 0 else ""
            lines.append(f"- **{r['ticker']}**: {_money(r['value'])}, "
                         f"{_pct(r['value'] / equity)} of equity{gain}")
        text = (f"{LABEL}\n\n**{top['ticker']} is the riskiest position** by "
                "size:\n\n" + "\n".join(lines) + "\n\n"
                f"A 20% drop in {top['ticker']} would cost about "
                f"{_pct(top['value'] * Decimal('0.2') / equity)} of the account.")
        if cash is not None:
            text += (f" Cash is {_pct(cash / equity)} of equity, so "
                     "concentration is the risk here, not leverage.")
        return LLMResponse(text=text)

    def _draft(self, question, results, available) -> LLMResponse:
        ticker = _draft_ticker(question)
        if "get_quote" in available and "get_quote" not in results:
            return self._call(("get_quote", {"ticker": ticker}))
        quote = _QUOTE.match(results.get("get_quote", ""))
        if quote is None:
            return LLMResponse(text=(
                f"{LABEL}\n\nI could not get a sample price for {ticker} "
                f"({results.get('get_quote', 'no quote tool')}). The keyless "
                "demo only knows a few tickers: try NVDA, AAPL, MSFT or AMZN."))
        trigger = (Decimal(quote["price"]) * PULLBACK).quantize(Decimal(1))
        if "draft_strategy" in available and "draft_strategy" not in results:
            return self._call(("draft_strategy", {
                "strategy_id": f"{ticker.lower()}-pullback-entry",
                "yaml_text": _draft_yaml(ticker, trigger),
                "reason": (f"Keyless demo draft: starter {ticker} position "
                           f"only after a 5% pullback (under {trigger}).")}))
        outcome = results.get("draft_strategy", "")
        queued = _QUEUED.search(outcome)
        if queued is None:
            return LLMResponse(text=(
                f"{LABEL}\n\nThe draft was not queued: "
                f"{outcome or 'the draft tool is not available here'}"))
        return LLMResponse(text=(
            f"{LABEL}\n\nI drafted **{ticker} - starter position on a "
            f"pullback** and queued it as #{queued[1]}:\n\n"
            f"- **Entry:** buy ${STARTER_NOTIONAL:,} only if {ticker} falls "
            f"under {trigger}, 5% below the sample price of {quote['price']}.\n"
            "- **Authorization:** `confirm`, so even after the rule fires the "
            "order waits for your click.\n"
            "- **Status:** `draft`. It is not saved or watched yet.\n\n"
            "Open **Pending** to read the YAML diff and approve or reject it."))


def build_demo_llm(settings, tier: str = "chat", usage_store=None) -> LLMClient:
    """`build_llm` for the demo: the real client when a key is configured
    (exactly what `serve` would build), the scripted one only when none is."""
    from allpath_trade.llm.factory import LLMConfigError, build_llm

    try:
        return build_llm(settings, tier=tier, usage_store=usage_store)
    except LLMConfigError:
        return DemoLLM()


def seed_sample_chat(conn: sqlite3.Connection) -> bool:
    """Put one example exchange in the paper chat so the page is not empty on
    first open. Skipped when the account already has a conversation."""
    from allpath_trade.store.conversations import ConversationStore

    store = ConversationStore(conn, account="paper")
    if store.latest() is not None:
        return False
    conversation_id = store.start()
    for message in SAMPLE_CHAT:
        store.append(conversation_id, dict(message))
    return True
