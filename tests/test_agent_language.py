"""AGENT_LANGUAGE / SHADOW_AGENT_LANGUAGE (allpath_trade/i18n.py): Chinese
for what the agent sends the owner on one account. English (the default)
must build every message exactly as before; tickers, numbers and callback
data are never translated. All sample text here is made up."""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from pydantic import ValidationError

from allpath_trade import card_brief, compact_cards, i18n
from allpath_trade.agent.context import OUTPUT_LANGUAGE_NOTE
from allpath_trade.agent.review import ReviewAnalysis
from allpath_trade.checkin import build_checkin_message
from allpath_trade.notify import dispatch, events
from allpath_trade.research import regime_metrics
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue
from tests.test_agent_loop import ScriptedLLM
from tests.test_card_brief import ORDER_KW, _client, _settings
from tests.test_compact_cards import (
    CRDO_COMPACT,
    CRDO_KW,
    NEW_TSLA,
    OLD_TSLA,
    _reflection_run,
)
from tests.test_sentinel import make, strategy_yaml

CJK = re.compile(r"[一-鿿]")
BRIEF_ZH = {"why": "上周下调了营收指引，这次下跌不只是噪音。更多细节在后面。",
            "risk": "现在加仓会加深已经亏损 22% 的仓位。",
            "plan": "只有逻辑成立时才允许按阶梯加仓。", "origin": "review_agent"}


# --- setting ----------------------------------------------------------------

def test_language_defaults_to_english_and_the_shadow_setting_only_changes_shadow(monkeypatch):
    s = _settings()
    assert i18n.account_language(s, "paper") == "en"
    assert i18n.account_language(s, "shadow") == "en"
    s = _settings(shadow_agent_language="zh")
    assert i18n.account_language(s, "shadow") == "zh"
    assert i18n.account_language(s, "paper") == "en"
    monkeypatch.setenv("SHADOW_AGENT_LANGUAGE", "ZH")
    assert _settings().shadow_agent_language == "zh" and _settings().agent_language == "en"
    # The system locale variable is not this setting.
    monkeypatch.setenv("LANGUAGE", "en_US:en")
    assert _settings().agent_language == "en"
    with pytest.raises(ValidationError):
        _settings(shadow_agent_language="fr")
    assert i18n.account_language(object(), "shadow") == "en"


def test_app_sets_the_language_on_shadow_only(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    client = _client(tmp_path, monkeypatch, shadow_agent_language="zh",
                     shadow_card_reason_brief=True)
    c = client.app.state.holder.get()
    assert c.accounts["paper"].queue.language == "en"
    assert c.accounts["paper"].sentinel.review_agent.language == "en"
    assert c.accounts["shadow"].queue.language == "zh"
    assert c.accounts["shadow"].sentinel.review_agent.language == "zh"
    client.__exit__(None, None, None)


# --- counting ---------------------------------------------------------------

def test_text_units_is_split_for_english_and_half_a_word_per_cjk_character():
    english = "Adding now deepens a position already down 22%."
    assert i18n.text_units(english) == len(english.split())
    assert i18n.text_units("") == 0
    assert i18n.text_units("现在加仓会加深亏损") == 5          # 9 characters
    assert i18n.text_units("NVDA 跌到 $135.35，低于止损") == 2 + 3   # 2 runs + 6 chars
    forty = "仓" * 40
    assert i18n.text_units(forty) == 20


def test_one_sentence_cuts_chinese_at_its_own_full_stop_and_caps_its_length():
    assert compact_cards.one_sentence(BRIEF_ZH["why"]) == "上周下调了营收指引，这次下跌不只是噪音。"
    assert compact_cards.one_sentence("价格跌破 $135.35！要不要卖？") == "价格跌破 $135.35！"
    long = "这" * 80 + "。"
    cut = compact_cards.one_sentence(long)
    assert cut.endswith("…") and i18n.text_units(cut[:-1]) <= compact_cards.MAX_WORDS
    # A number is never cut in half.
    edge = "仓" * 54 + "$135.35 是止损位，跌破就卖出全部仓位不再等待。"
    assert not re.search(r"\d…$", compact_cards.one_sentence(edge).replace("$135.35…", "…"))
    # English text takes the old path.
    assert compact_cards.one_sentence("First one. Second one.") == "First one."


def test_brief_limits_and_number_check_work_on_chinese():
    sources = ["NVDA fell to $135.35 on 2026-11-20; the position is 15,000 dollars, down 22%."]
    ok = card_brief.accept(
        {"why": "11月20日跌到 $135.35，仓位 1.5万 美元。", "risk": "已经亏损 22%，再跌会更深。"},
        sources=sources, origin="review_agent")
    assert ok.brief is not None and ok.reason == ""
    bad = card_brief.accept(
        {"why": "跌到 $120，仓位 3万 美元。", "risk": "已经亏损 22%。"},
        sources=sources, origin="review_agent")
    assert bad.brief is None and "120" in bad.reason and "3万" in bad.reason
    # 200 characters per line = 100 words each: over the 80-word limit, though
    # str.split() would count each line as one word.
    long = card_brief.accept({"why": "仓" * 200, "risk": "险" * 200},
                             sources=sources, origin="review_agent")
    assert long.brief is None and "words" in long.reason
    # English numbers still read the old way.
    assert card_brief.ungrounded("about $15k and 22%", sources) == []


# --- code-built cards ---------------------------------------------------------

def test_compact_order_card_in_chinese_keeps_ticker_numbers_and_structure():
    subject, body = events.review_queued(**CRDO_KW, brief=BRIEF_ZH, compact=CRDO_COMPACT,
                                         lang="zh")
    assert subject == "[Shadow] CRDO 买入 $1,500 — Agent 建议跳过"
    assert body.split("\n") == [
        "CRDO · 买入 $1,500（~11 股 @ $135.35）",
        "Agent 建议：跳过",
        "",
        "原因：上周下调了营收指引，这次下跌不只是噪音。",
        "风险：现在加仓会加深已经亏损 22% 的仓位。",
        "",
        "Shadow：批准只是记账，订单要你自己在券商下。"]
    assert body.lang == "zh" and body.card_format == "compact"
    assert body.email.endswith("24 小时内有效。")
    # The event for the machine channel is the same in either language.
    assert subject.event == events.review_queued(**CRDO_KW, brief=BRIEF_ZH,
                                                 compact=CRDO_COMPACT)[0].event


def test_classic_order_card_in_chinese():
    subject, body = events.review_queued(**CRDO_KW, brief=BRIEF_ZH, lang="zh")
    assert subject == "[Shadow] [AllPath] CRDO：等你批准"
    lines = body.split("\n")
    assert lines[:5] == ["第 #276 条在等你处理。", "提议：CRDO buy $1500", "策略：crdo-ladder",
                         "触发时价格：$135.35", "估算数量：按该价约 11.08 股"]
    assert lines[5].startswith("原因：") and lines[6].startswith("风险：")
    assert lines[7].startswith("计划：") and lines[8] == "Agent 建议：跳过"
    assert "批准后要你自己去下单" in lines[9]
    assert body.lang == "zh" and not hasattr(body, "card_format")


def test_rule_messages_in_chinese():
    kw = {"account": "shadow", "strategy_id": "fig-watch", "rule_id": "alert-20",
          "ticker": "FIG", "condition": "price <= 20"}
    subject, body = events.rule_triggered(**kw, disposition="notified", compact=True,
                                          price=Decimal("19.8"), lang="zh")
    assert subject == "[Shadow] FIG 提醒：$19.80（价格 ≤ $20）"
    assert body == "FIG 跌到 $19.80（你的提醒：价格 ≤ $20）。无需批准。"
    assert body.lang == "zh"
    _s, body = events.rule_triggered(
        account="shadow", strategy_id="tsla-swing", rule_id="stop", ticker="TSLA",
        condition="position_weight > 0 and price < 315", disposition="skipped",
        compact=True, price=Decimal("310.2"), reason="你没有可卖的 TSLA", lang="zh")
    assert body == ("TSLA：规则 stop 触发，价格 $310.20（你有持仓 且 价格 < $315），"
                    "但没有生成订单：你没有可卖的 TSLA。无需批准。")
    subject, body = events.rule_triggered(**kw, disposition="notified", lang="zh")
    assert subject == "[Shadow] [AllPath] FIG：规则 alert-20 触发"
    assert body.startswith("策略 fig-watch 的规则 alert-20 在 FIG 上触发。\n条件：price <= 20\n处理：已提醒。")


def test_revision_card_in_chinese_lists_the_same_changes():
    english = compact_cards.revision_changes(OLD_TSLA, NEW_TSLA)
    chinese = compact_cards.revision_changes(OLD_TSLA, NEW_TSLA, lang="zh")
    assert english == ["dip-buy-1: turned off", "trim-strength: trigger weight > 14% → 12%"]
    assert chinese == ["dip-buy-1：已关闭", "trim-strength：触发条件 仓位占比 > 14% → 12%"]
    assert [c.key for c in chinese] == [c.key for c in english] == [True, False]
    subject, body = compact_cards.revision_card(
        account="shadow", review_id=31, ticker="TSLA", strategy_id="tsla-swing",
        old_yaml=OLD_TSLA, new_yaml=NEW_TSLA, rationale="区间变窄了，旧的减仓位很少触及。后面还有。",
        effect="x", web_base_url="https://x.test", prefix="[Shadow] ", lang="zh")
    assert subject == "[Shadow] tsla-swing — 提议 2 处策略修改"
    assert body.split("\n")[:6] == [
        "TSLA · tsla-swing · 策略修改", "Agent 提议 2 处修改：", "• dip-buy-1：已关闭",
        "• trim-strength：触发条件 仓位占比 > 14% → 12%", "现在批准的效果：x",
        "原因：区间变窄了，旧的减仓位很少触及。"]
    assert body.split("\n")[-1] == "完整 diff：https://x.test/reviews#review-31"
    new = compact_cards.revision_changes("", NEW_TSLA, is_new=True, lang="zh")
    assert new[0] == "授权：confirm（每笔订单都先问你）" and new[1] == "批准后的状态：active"
    assert "规则 stop：你有持仓 且 价格 < $250 时 sell all" in new


def test_receipt_and_digest_in_chinese_and_english_unchanged():
    kw = {"account": "shadow", "ticker": "NVDA", "side": "sell", "submitted": True,
          "detail": "x", "filled_qty": Decimal(10), "filled_avg_price": Decimal("135.35")}
    subject, body = events.order_result(**kw, lang="zh")
    assert subject == "[Shadow] [AllPath] NVDA：已记账，请自己下单"
    assert body.startswith("NVDA 的卖出订单已记入 shadow 账本，请现在去券商下单：卖出 10 NVDA @ ~$135.35。\nx")
    assert events.order_result(**kw)[0] == "[Shadow] [AllPath] NVDA: order recorded — place it yourself"
    subject, body = events.daily_digest(account="shadow", triggers=2, trades=1, pending=3,
                                        lang="zh")
    assert subject == "[Shadow] [AllPath] 每日小结"
    assert body.startswith("Shadow 账户今天：规则触发 2 次，记账 1 笔，还有 3 条等你批准。")
    subject, body = events.daily_report(account="shadow", date="2026-11-20",
                                        summary="小结", body="正文", lang="zh")
    assert subject == "[Shadow] [AllPath] 每日复盘 2026-11-20" and body.startswith("小结\n\n正文")


def test_checkin_frame_in_chinese_and_english_unchanged():
    assert build_checkin_message("2026-11-20", ["Q1?"]) == (
        "[Shadow] Evening check-in (2026-11-20)\n\n1. Q1?\n\n"
        "Reply to this message -- a line or two is enough. Skip any you don't care about.")
    assert build_checkin_message("2026-11-20", ["问题一？"], "zh") == (
        "[Shadow] 晚间 check-in（2026-11-20）\n\n1. 问题一？\n\n回复本条即可，一两句就够，不关心的可以跳过。")


def test_every_translated_template_keeps_its_placeholders():
    for english, chinese in i18n._ZH.items():
        fields = sorted(re.findall(r"\{(\w+)\}", english))
        assert sorted(re.findall(r"\{(\w+)\}", chinese)) == fields, english
    assert i18n.tr("zh", "no such template {x}", x=1) == "no such template 1"
    assert i18n.tr("en", "Why: {text}", text="a") == "Why: a"


# --- through the sentinel and Telegram ---------------------------------------

class _Agent:
    def analyze(self, review):
        analysis = ReviewAnalysis(recommendation="execute", reasoning="趋势还在。后面还有细节。")
        analysis.brief = None
        return analysis


class _API:
    sent: list = []

    def __init__(self, token):
        pass

    def send_message(self, chat_id, html, reply_markup=None):
        _API.sent.append((html, reply_markup))
        return True


def _run(tmp_path, monkeypatch, account, lang):
    monkeypatch.setattr(dispatch, "TelegramAPI", _API)
    _API.sent = []
    s, _store, _ex, q, notifier = make(tmp_path, strategy_yaml(auth="confirm"),
                                       account=account)
    q.language = lang
    q.compact_cards = True
    s.review_agent = _Agent()
    s.app_state = AppState(q._conn)
    s.app_state.set(TELEGRAM_CHAT_ID_KEY, "42")
    s.telegram_bot_token = "t"
    s.run_once()
    return q, notifier.sent, _API.sent


def test_shadow_trigger_sends_a_chinese_card_and_records_its_language(tmp_path, monkeypatch):
    q, sent, pushed = _run(tmp_path, monkeypatch, "shadow", "zh")
    [(subject, body)] = sent
    assert subject.startswith("[Shadow] AAPL ") and "Agent 建议执行" in subject
    assert "Agent 建议：执行" in body and "原因：趋势还在。" in body
    row = q.get(1)
    assert row["card_lang"] == "zh" and row["card_format"] == "compact"
    [(html, markup)] = pushed
    assert html.startswith("[Shadow] AAPL · ")
    buttons = markup["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ 批准", "❌ 拒绝"]
    assert [b["callback_data"].split(":")[:3] for b in buttons] == [
        ["rv", "approve", "1"], ["rv", "reject", "1"]]


def test_paper_trigger_is_unchanged_and_records_no_language(tmp_path, monkeypatch):
    q, sent, pushed = _run(tmp_path, monkeypatch, "paper", "en")
    [(subject, body)] = sent
    # Only the (fake) agent's own sentence is Chinese; every code-built word is English.
    assert not CJK.search(subject + body.replace("趋势还在。", ""))
    assert "Agent: EXECUTE" in body and not hasattr(body, "lang") or body.lang == "en"
    assert q.get(1)["card_lang"] is None
    assert [b["text"] for b in pushed[0][1]["inline_keyboard"][0]] == ["✅ Approve", "❌ Reject"]


def test_card_formats_export_has_the_language(tmp_path, monkeypatch):
    q, _sent, _pushed = _run(tmp_path, monkeypatch, "shadow", "zh")
    q._conn.execute("UPDATE pending_reviews SET ts = '2026-11-20T15:00:00+00:00'")
    rows = regime_metrics.card_format_rows(q._conn, "2026-11-01", "2026-11-30")
    assert regime_metrics.CARD_FORMAT_COLUMNS[-1] == "card_lang"
    assert [(r["card_format"], r["card_lang"]) for r in rows] == [("compact", "zh")]
    queue = ReviewQueue(connect(":memory:"), None, account="paper")
    assert queue.language == "en"


# --- model-written text -------------------------------------------------------

def test_reflection_prompt_gets_the_language_note_for_shadow_only(tmp_path, monkeypatch):
    from tests import test_reflect

    for name in ("paper", "shadow_en", "shadow_zh"):
        (tmp_path / name).mkdir()
    real = test_reflect.make_components

    def with_language(path, account="paper"):
        components = real(path, account=account)
        components.queue.language = "zh" if path.name == "shadow_zh" else "en"
        return components

    monkeypatch.setattr(test_reflect, "make_components", with_language)
    paper, _ = _reflection_run(tmp_path / "paper", "paper")
    shadow_en, _ = _reflection_run(tmp_path / "shadow_en", "shadow")
    shadow_zh, _ = _reflection_run(tmp_path / "shadow_zh", "shadow")
    note = json.dumps(i18n.LANGUAGE_NOTE_ZH)[1:-1]
    english = json.dumps(OUTPUT_LANGUAGE_NOTE)[1:-1]
    assert note not in paper and note not in shadow_en
    assert english in paper and english in shadow_en
    assert shadow_zh.count(note) == 2 and english not in shadow_zh


def test_review_agent_prompt_gets_the_instruction_only_when_not_english():
    from allpath_trade.agent.review import ReviewAgent
    from allpath_trade.agent.tools import ToolRegistry
    from allpath_trade.llm.base import LLMResponse

    class Seen:
        def __init__(self):
            self.prompts = []

        def complete(self, messages, tools=None):
            self.prompts.append(messages[0]["content"])
            return LLMResponse(text='{"recommendation": "skip", "reasoning": "x"}')

    review = {"strategy_id": "s", "rule_id": "r", "rule_type": "soft", "ticker": "NVDA",
              "condition": "price < 1", "action": "buy $1", "snapshot": "{}"}
    en, zh = Seen(), Seen()
    ReviewAgent(en, ToolRegistry()).analyze(review)
    ReviewAgent(zh, ToolRegistry(), language="zh").analyze(review)
    assert zh.prompts[0] == en.prompts[0] + i18n.LANGUAGE_INSTRUCTION_ZH
    assert "Simplified Chinese" not in en.prompts[0]


def test_classic_english_card_is_the_same_object_shape_as_before():
    subject, body = events.review_queued(**ORDER_KW)
    assert type(body) is str and not hasattr(body, "lang")
    assert events.review_queued(**ORDER_KW, lang="en") == (subject, body)


# --- the strings that were still English on shadow ----------------------------

def test_stop_warning_drawdown_alerts_ledger_titles_and_account_switch_in_chinese():
    from allpath_trade.strategy.loader import parse_strategy_text
    from allpath_trade.strategy.stops import stop_warning

    doc = parse_strategy_text("t", """
name: T
status: active
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: stop, type: hard, condition: "price < 90", action: "sell all", state: triggered}
""")
    english = stop_warning(doc)
    assert english.startswith("No armed stop: t/stop (price < 90), spent. This buy adds")
    assert stop_warning(doc, None, "en") == english
    assert stop_warning(doc, None, "zh") == (
        "没有待触发的止损：t/stop (price < 90), spent。这笔买入加的是止损已经不再保护的仓位；"
        "请想清楚后重新启用或修改止损。")

    kw = {"account": "shadow", "peak": Decimal(100000), "equity": Decimal(84000),
          "drawdown": Decimal("0.16"), "demoted": ["zzz-swing"]}
    subject, body = events.drawdown_halt(**kw, lang="zh")
    assert subject == "[Shadow] [AllPath] 交易已暂停：回撤 16.0%"
    assert body.startswith("净值 $84,000.00，比峰值 $100,000.00 低 16.0%。\n回撤熔断已触发。")
    assert "zzz-swing" in body and "breaker reset --account shadow" in body
    assert subject.event == events.drawdown_halt(**kw)[0].event
    assert events.drawdown_halt(**kw)[1].startswith("Equity $84,000.00 is 16.0% below")
    subject, body = events.drawdown_deepening(
        account="shadow", peak=Decimal(100000), equity=Decimal(79000),
        drawdown=Decimal("0.21"), level=Decimal("0.20"), tripped_at="2026-11-20T15:00:00",
        lang="zh")
    assert subject == "[Shadow] [AllPath] 回撤仍在扩大：比峰值低 21.0%"
    assert "已越过 20% 这一档" in body and "2026-11-20T15:00 UTC" in body

    for title, want in (
            ("Set position ZZZ", "设置持仓 ZZZ"), ("Set cash", "设置现金"),
            ("Remove position ZZZ", "移除持仓 ZZZ"), ("Correct fill #12", "更正成交 #12"),
            ("Reset ledger", "清空账本"),
            ("Import 3 position(s) from CSV", "从 CSV 导入 3 个持仓"),
            ("Set option ZZZ 2027-01-15 $150 call", "设置期权持仓 ZZZ 2027-01-15 $150 call"),
            ("Mark option ZZZ 2027-01-15 $150 call at $14.10",
             "期权 ZZZ 2027-01-15 $150 call 现价记为 $14.10"),
            ("持仓同步：3 处变化", "持仓同步：3 处变化"), ("something else", "something else")):
        assert i18n.ledger_title("zh", title) == want
        assert i18n.ledger_title("en", title) == title
    subject, body = events.review_queued(
        account="shadow", review_id=5, ticker="", action="Set cash", strategy_id="",
        kind="shadow_edit", lang="zh")
    assert "提议：设置现金" in body
    # An order's free-text action is never run through the title table.
    _s, body = events.review_queued(account="shadow", review_id=5, ticker="ZZZ",
                                    action="Set cash", strategy_id="", kind="order", lang="zh")
    assert "提议：ZZZ Set cash" in body


def test_account_switch_messages_follow_the_target_accounts_language(tmp_path):
    from types import SimpleNamespace

    from tests.test_telegram_poller import (
        FakeChatService,
        FakeTelegramAPI,
        _callback_update,
        _update,
        make_app_state,
        make_poller,
        pair,
    )

    app_state = make_app_state(tmp_path)
    pair(app_state, 42)
    api = FakeTelegramAPI()
    poller = make_poller(api, FakeChatService(), app_state)
    real = poller.holder.get

    def get():
        c = real()
        c.accounts = {"paper": SimpleNamespace(queue=SimpleNamespace(language="en")),
                      "shadow": SimpleNamespace(queue=SimpleNamespace(language="zh"))}
        return c

    poller.holder.get = get
    poller._handle_update(_update(1, 42, "/account"))          # the chat defaults to shadow
    assert api.sent_messages[-1][1] == (
        "这个聊天现在连的是 <b>Shadow</b>。选择它要连哪个账户：")
    poller._handle_update(_callback_update(2, 42, 42, "acct:paper"))
    assert api.sent_messages[-1][1] == (
        "Switched to <b>Paper</b>. This chat now talks to your paper account.")
    assert api.answered_callbacks[-1][1] == "Switched to Paper"
    poller._handle_update(_callback_update(3, 42, 42, "acct:shadow"))
    assert api.sent_messages[-1][1] == "已切换到 <b>Shadow</b>。这个聊天现在连的是你的 shadow 账户。"
    assert api.answered_callbacks[-1][1] == "已切换到 Shadow"
