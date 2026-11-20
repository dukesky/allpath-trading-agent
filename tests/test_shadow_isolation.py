"""Shadow -> paper isolation (TDSC red-team F5/M6, and the condition for the
shadow-advisor features): nothing the shadow account writes -- holdings,
option lines, memory, views, chat, ledger edits, Chinese text -- may reach
any prompt, tool list or notification of the PAPER account.

One test builds the app twice: once with every new shadow setting off and no
shadow data, once with all of them on and shadow data written through every
store. It then assembles what the paper account's models and the owner see
(chat prompt and tools, reflection messages and tools, review prompt,
consolidation prompt, rule and digest notifications) and requires the two to
be byte-identical. All data is invented; MARK is the canary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from allpath_trade.llm.base import LLMClient, LLMResponse
from allpath_trade.store.views import ViewStore
from tests.test_card_brief import SpyNotifier, _client

MARK = "SHADOWCANARY"
NOW = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)
REPORT = "REPORT\nDay summary.\nSUMMARY\nA quiet day."

# Every per-account setting added for the shadow advisor work. A branch that
# adds one must add it here, so this test keeps covering it.
SHADOW_SETTINGS = {
    "shadow_option_holdings": True,
    "shadow_agent_language": "zh",
    "shadow_daily_checkin": True,
    "shadow_card_reason_brief": True,
    "shadow_compact_cards": True,
    "shadow_concise_replies": True,
    "shadow_telegram_order_threads": True,
    "shadow_reflection_revision_cards": True,
}

PAPER_STRATEGY = """
name: "Paper alert"
status: active
authorization: notify
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: soft, condition: "price > 0", action: "sell 10%"}
"""
SHADOW_STRATEGY = f"""
name: "{MARK} shadow plan"
status: active
authorization: notify
thesis: "{MARK} 真实账户的计划"
position: {{ticker: ZZZ, target_weight: 10%}}
rules:
  - {{id: r1, type: soft, condition: "price > 0", action: "sell 10%"}}
"""


class Recorder(LLMClient):
    """Returns one canned reply forever and keeps what it was sent."""

    model = "recorder"

    def __init__(self, text):
        self.text = text
        self.seen = []
        self.tools = []

    def complete(self, messages, tools=None):
        self.seen.append(json.dumps(messages, default=str, ensure_ascii=False))
        self.tools.append(json.dumps(
            [t.model_dump() if hasattr(t, "model_dump") else t for t in tools or []],
            default=str, sort_keys=True))
        return LLMResponse(text=self.text)


def write_shadow_data(client, tmp_path):
    """Put the canary into every store the shadow account owns."""
    c = client.app.state.holder.get()
    shadow = c.accounts["shadow"]
    shadow.broker._data = c.data          # the fake data source: no network in tests
    (shadow.strategies.directory).mkdir(parents=True, exist_ok=True)
    (shadow.strategies.directory / "zzz-plan.yaml").write_text(SHADOW_STRATEGY)
    shadow.broker.set_cash(Decimal(50000))
    shadow.broker.set_position("ZZZ", Decimal(40), Decimal("182.5"))
    if hasattr(shadow.broker, "set_option_position"):
        shadow.broker.set_option_position("ZZZ270115C00150000", Decimal(2), Decimal("12.30"),
                                          Decimal("14.10"))
    for layer, key in (("profile", None), ("stock", "ZZZ"), ("lesson", "sizing"),
                       ("strategy", "zzz-plan")):
        shadow.memory.apply(layer, key, "add",
                            text=f"{MARK} holds 40 ZZZ at the real broker 真实持仓")
    ViewStore(c.conn, "shadow").add(
        subject="VISION", stance=f"{MARK} own compounders for ten years 长期持有",
        horizon="long-term", conviction="high", invalidation="none", source="chat")
    shadow.observations.add("sentinel", f"{MARK} shadow observation")
    shadow.queue.add_shadow_edit(op="set_cash", ticker="", action=f"{MARK} Set cash",
                                 args={"amount": "1"}, before={"cash": "50000"},
                                 after={"cash": "1"})
    client.app.state.chat_services["shadow"].note_resolution(
        f"{MARK} I bought 10 ZZZ at 182.5 我买了", source="telegram")
    shadow.reports.add(date="2026-11-19", body=f"{MARK} 复盘", summary=f"{MARK} 小结",
                       conversation_id=None, model="m", tokens_used=0, status="ok")


def paper_surface(client, tmp_path) -> dict[str, str]:
    """Everything the paper account's models and the owner are shown."""
    from allpath_trade import scheduler

    c = client.app.state.holder.get()
    paper = c.accounts["paper"]
    out: dict[str, str] = {}

    session = client.app.state.chat_services["paper"].session()
    out["chat_prompt"] = session.system_prompt
    out["chat_tools"] = json.dumps([s.model_dump() for s in session.registry.specs()],
                                   default=str, sort_keys=True)

    reflection = Recorder(REPORT)
    paper.reflector.llm = reflection
    paper.reflector.run_daily(now=NOW)
    out["reflection_messages"] = reflection.seen[0]
    out["reflection_tools"] = reflection.tools[0]

    review = Recorder('{"recommendation": "skip", "reasoning": "x"}')
    paper.sentinel.review_agent.llm = review
    paper.sentinel.review_agent.analyze({
        "strategy_id": "paper-alert", "rule_id": "r1", "rule_type": "soft",
        "ticker": "AAPL", "condition": "price > 0", "action": "sell 10%", "snapshot": "{}"})
    out["review_prompt"] = review.seen[0]
    out["review_tools"] = review.tools[0]

    consolidation = Recorder("nothing to change")
    paper.consolidator.llm = consolidation
    paper.observations.add("sentinel", "paper observation")
    paper.consolidator.run_daily()
    out["consolidation"] = "\n".join(consolidation.seen)

    spy = SpyNotifier()
    paper.sentinel.notifier = spy
    paper.sentinel.run_once()
    out["rule_notifications"] = json.dumps(spy.sent, ensure_ascii=False)

    digest = SpyNotifier()
    digest.send = lambda subject, body: digest.sent.append((subject, body)) or True
    c.notifier = digest
    scheduler._send_daily_digest(c)
    out["paper_digest"] = json.dumps(
        [m for m in digest.sent if str(m[0]).startswith("[Paper]")], ensure_ascii=False)

    out["memory_profile"] = paper.memory.render_for_context("profile")
    # Web pages of the paper view: checked for the canary only (the account
    # switcher legitimately shows a dot when the OTHER account has items
    # waiting, so the pages are not byte-identical by design).
    out["page:dashboard"] = client.get("/").text
    out["page:pending"] = client.get("/reviews").text
    out["page:memory"] = client.get("/memory").text
    out["page:chat"] = client.get("/chat").text
    root = str(tmp_path)
    return {k: v.replace(root, "<tmp>") for k, v in out.items()}


def build(tmp_path, monkeypatch, sub, *, shadow_on):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: Recorder("ok"))
    path = tmp_path / sub
    path.mkdir()
    client = _client(path, monkeypatch, **(SHADOW_SETTINGS if shadow_on else {}))
    c = client.app.state.holder.get()
    (c.accounts["paper"].strategies.directory).mkdir(parents=True, exist_ok=True)
    (c.accounts["paper"].strategies.directory / "paper-alert.yaml").write_text(PAPER_STRATEGY)
    c.accounts["paper"].memory.apply("profile", None, "add", text="Risk tolerance: moderate")
    if shadow_on:
        write_shadow_data(client, path)
    surface = paper_surface(client, path)
    client.__exit__(None, None, None)
    return surface


def test_paper_surface_is_identical_with_shadow_features_on_and_shadow_data_written(
        tmp_path, monkeypatch):
    off = build(tmp_path, monkeypatch, "off", shadow_on=False)
    on = build(tmp_path, monkeypatch, "on", shadow_on=True)
    assert set(off) == set(on)
    for name in off:
        assert MARK not in on[name], f"shadow data leaked into paper's {name}"
        assert "真实" not in on[name] and "长期" not in on[name], name
        if not name.startswith("page:"):
            assert on[name] == off[name], f"paper's {name} differs with shadow features on"
    # The surface is real: each piece has content, and the notifications fired.
    assert "Risk tolerance: moderate" in off["chat_prompt"]
    assert "AAPL" in off["rule_notifications"] and "[Paper]" in off["paper_digest"]
    assert len(off["reflection_messages"]) > 500 and len(off["review_prompt"]) > 200


def test_the_canary_does_reach_shadow_so_the_test_can_see_a_leak(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: Recorder("ok"))
    client = _client(tmp_path, monkeypatch, **SHADOW_SETTINGS)
    write_shadow_data(client, tmp_path)
    prompt = client.app.state.chat_services["shadow"].session().system_prompt
    assert MARK in prompt and "真实持仓" in prompt
    assert "option: ZZZ 2027-01-15 $150 call" in prompt
    client.__exit__(None, None, None)
