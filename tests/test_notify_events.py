from allpath_trade.notify import events
from tests.helpers import assert_english_only


def test_bodies_contain_no_links_or_html():
    subject, body = events.review_queued(
        account="paper", review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        recommendation="execute")
    assert "http" not in body.lower()
    assert "<" not in body
    assert "12" in body and "AAPL" in subject


def test_bodies_are_english_only():
    for subject, body in [
        events.rule_triggered(account="paper", strategy_id="s", rule_id="r", ticker="AAPL",
                              condition="price < 100", disposition="queued"),
        events.order_result(account="paper", ticker="AAPL", side="buy", submitted=True,
                            detail="filled 3 @ 220.15"),
        events.daily_digest(account="paper", triggers=2, trades=1, pending=3),
    ]:
        for text in (subject, body):
            assert_english_only(text)


# -- Approve-by-link (Part A): the footer must stay truthful either way --
# it's appended to every notification, whether or not that particular one
# ends up carrying a link.


def test_footer_no_longer_claims_no_links_by_design():
    _, body = events.rule_triggered(account="paper", strategy_id="s", rule_id="r", ticker="AAPL",
                                    condition="price < 100", disposition="queued")
    assert "no links by design" not in body
    assert "never carries your dashboard access token" in body


def test_review_queued_without_approve_url_carries_no_link():
    _subject, body = events.review_queued(
        account="paper", review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1")
    assert "http" not in body.lower()
    assert "Review & approve" not in body


def test_review_queued_with_approve_url_includes_it_once():
    _subject, body = events.review_queued(
        account="paper", review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        approve_url="http://192.168.1.20:8791/a/12?k=abc123")
    assert "Review & approve: http://192.168.1.20:8791/a/12?k=abc123" in body
    assert body.count("http://192.168.1.20:8791/a/12?k=abc123") == 1


def test_review_queued_price_context_is_included_when_present():
    _subject, body = events.review_queued(
        account="paper", review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        trigger_price="$204.50", est_shares="2.44")
    assert "Price at trigger: $204.50" in body
    assert "Est. size: ~2.44 shares at that price" in body


def test_review_queued_with_empty_ticker_uses_ledger_as_subject_noun():
    # M2: a ticker-less shadow ledger edit (set_cash/import/reset) used to
    # leave a bare "[AllPath] : waiting for your approval" subject and a
    # dangling "Proposed: ... on " body line -- "Ledger" stands in as the
    # subject noun instead of an empty string.
    subject, body = events.review_queued(
        account="paper", review_id=12, ticker="", action="Reset ledger", strategy_id="")
    assert subject == "[Paper] [AllPath] Ledger: waiting for your approval"
    assert "Proposed: Reset ledger" in body
    assert "Proposed: Reset ledger on" not in body


def test_review_queued_price_context_is_omitted_when_absent():
    _subject, body = events.review_queued(
        account="paper", review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1")
    assert "Price at trigger" not in body
    assert "Est. size" not in body


def test_review_queued_with_approve_url_is_english_only():
    subject, body = events.review_queued(
        account="paper", review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        trigger_price="$204.50", est_shares="2.44",
        approve_url="http://192.168.1.20:8791/a/12?k=abc123")
    for text in (subject, body):
        assert_english_only(text)


# -- approve_link: shared by sentinel.py and agent/action_tools.py --


class _Handle(int):
    def __new__(cls, value, token=None):
        obj = super().__new__(cls, value)
        obj.token = token
        return obj


def test_approve_link_empty_when_base_url_unset():
    assert events.approve_link("", _Handle(12, "tok")) == ""


def test_approve_link_empty_when_no_token():
    assert events.approve_link("http://192.168.1.20:8791", _Handle(12, None)) == ""
    assert events.approve_link("http://192.168.1.20:8791", 12) == ""


def test_approve_link_built_when_both_present():
    url = events.approve_link("http://192.168.1.20:8791", _Handle(12, "abc123"))
    assert url == "http://192.168.1.20:8791/a/12?k=abc123"


# ---------------------------------------------------------------------------
# daily_digest -- LLM cost line (Item B)
# ---------------------------------------------------------------------------

def test_daily_digest_omits_cost_line_by_default():
    _subject, body = events.daily_digest(account="paper", triggers=1, trades=0, pending=0)
    assert "LLM cost" not in body


def test_daily_digest_includes_cost_line_when_given():
    _subject, body = events.daily_digest(
        account="paper", triggers=1, trades=0, pending=0, llm_cost="$0.42")
    assert "Estimated LLM cost today (all accounts): $0.42" in body


def test_daily_digest_with_cost_line_is_english_only():
    _subject, body = events.daily_digest(
        account="paper", triggers=1, trades=0, pending=0, llm_cost="$0.42")
    assert_english_only(body)


def test_daily_digest_shadow_counts_orders_recorded():
    subject, body = events.daily_digest(account="shadow", triggers=2, trades=3, pending=1)
    assert subject.startswith("[Shadow] ")
    assert "Shadow account today: 2 rule trigger(s), 3 order(s) recorded, " \
        "1 item(s) still waiting for your approval." in body


def test_daily_digest_paper_names_account():
    subject, body = events.daily_digest(account="paper", triggers=2, trades=3, pending=1)
    assert subject.startswith("[Paper] ")
    assert "Paper account today: 2 rule trigger(s), 3 trade(s), " \
        "1 item(s) still waiting for your approval." in body


# ---------------------------------------------------------------------------
# shadow-dual-active T7: `[Paper]`/`[Shadow]` subject prefixes on every
# builder, shadow order/queued-review wording, and a missing/invalid
# account degrading to no prefix rather than crashing.
# ---------------------------------------------------------------------------

def test_rule_triggered_is_prefixed_per_account():
    subject, _ = events.rule_triggered(
        account="paper", strategy_id="s", rule_id="r", ticker="AAPL",
        condition="c", disposition="queued")
    assert subject.startswith("[Paper] ")
    subject, _ = events.rule_triggered(
        account="shadow", strategy_id="s", rule_id="r", ticker="AAPL",
        condition="c", disposition="queued")
    assert subject.startswith("[Shadow] ")


def test_order_result_paper_wording_unchanged():
    subject, body = events.order_result(
        account="paper", ticker="AAPL", side="buy", submitted=True,
        detail="filled 3 @ 220.15")
    assert subject.startswith("[Paper] ")
    assert "recorded in your shadow ledger" not in body
    assert "A buy order for AAPL was submitted." in body


def test_order_result_shadow_submitted_says_recorded_and_place_it_yourself():
    subject, body = events.order_result(
        account="shadow", ticker="TSLA", side="buy", submitted=True,
        detail="agent-approved; submitted")
    assert subject.startswith("[Shadow] ")
    assert "recorded in your shadow ledger" in body
    assert "place this order in your brokerage now" in body


def test_order_result_shadow_submitted_includes_fill_when_given():
    from decimal import Decimal

    _subject, body = events.order_result(
        account="shadow", ticker="TSLA", side="buy", submitted=True,
        detail="submitted", filled_qty=Decimal("4.5"),
        filled_avg_price=Decimal("332.01"))
    assert "place this order in your brokerage now: BUY 4.5 TSLA @ ~$332.01" in body


def test_order_result_shadow_rejected_keeps_generic_wording():
    # Nothing was recorded, so no "place it yourself" instruction applies.
    _subject, body = events.order_result(
        account="shadow", ticker="TSLA", side="buy", submitted=False,
        detail="risk gate rejected")
    assert "recorded in your shadow ledger" not in body
    assert "A buy order for TSLA was not submitted." in body


def test_review_queued_shadow_order_says_place_it_yourself():
    _subject, body = events.review_queued(
        account="shadow", review_id=12, ticker="TSLA", action="buy 1 TSLA",
        strategy_id="s1")
    assert "you'll place it yourself" in body


def test_review_queued_paper_order_has_no_shadow_clause():
    _subject, body = events.review_queued(
        account="paper", review_id=12, ticker="TSLA", action="buy 1 TSLA",
        strategy_id="s1")
    assert "you'll place it yourself" not in body


def test_review_queued_shadow_non_order_kind_has_no_shadow_clause():
    _subject, body = events.review_queued(
        account="shadow", review_id=12, ticker="", action="Reset ledger",
        strategy_id="", kind="shadow_edit")
    assert "you'll place it yourself" not in body


def test_daily_report_is_prefixed_per_account():
    subject, _ = events.daily_report(
        account="shadow", date="2026-08-21", summary="s", body="b")
    assert subject.startswith("[Shadow] ")


def test_unknown_account_degrades_to_no_prefix_not_a_crash(capsys):
    subject, _ = events.rule_triggered(
        account="bogus", strategy_id="s", rule_id="r", ticker="AAPL",
        condition="c", disposition="queued")
    assert subject == "[AllPath] AAPL: rule r triggered"
    assert "bogus" in capsys.readouterr().err


def test_missing_account_degrades_to_no_prefix_not_a_crash():
    subject, _ = events.rule_triggered(
        account="", strategy_id="s", rule_id="r", ticker="AAPL",
        condition="c", disposition="queued")
    assert subject == "[AllPath] AAPL: rule r triggered"


# ---------------------------------------------------------------------------
# C3: the shadow "recorded, not submitted" wording has to reach the SUBJECT
# too (the ntfy title is the same string) -- a body that says "place it
# yourself" under a subject that says "order submitted" is still a lie on
# the surface most readers see first.
# ---------------------------------------------------------------------------

def test_order_result_shadow_subject_never_says_submitted():
    subject, _body = events.order_result(
        account="shadow", ticker="AAPL", side="buy", submitted=True,
        detail="agent-approved; recorded")
    assert subject == "[Shadow] [AllPath] AAPL: order recorded — place it yourself"
    assert "submitted" not in subject


def test_order_result_paper_subject_unchanged():
    subject, _body = events.order_result(
        account="paper", ticker="AAPL", side="buy", submitted=True,
        detail="filled 3 @ 220.15")
    assert subject == "[Paper] [AllPath] AAPL: order submitted"


def test_order_result_shadow_rejected_subject_keeps_generic_wording():
    subject, _body = events.order_result(
        account="shadow", ticker="AAPL", side="buy", submitted=False,
        detail="risk gate rejected")
    assert subject == "[Shadow] [AllPath] AAPL: order not submitted"


def test_every_builder_is_english_only_for_both_accounts():
    # C3: the shadow variants added by this task (em dashes, "place it
    # yourself", the ledger digest line) go through the same sweep every
    # paper variant already did, so a future edit can't slip CJK into a
    # shadow-only branch that nothing checks.
    for account in ("paper", "shadow"):
        for subject, body in [
            events.rule_triggered(account=account, strategy_id="s", rule_id="r",
                                  ticker="AAPL", condition="price < 100",
                                  disposition="queued"),
            events.order_result(account=account, ticker="AAPL", side="buy",
                                submitted=True, detail="filled 3 @ 220.15"),
            events.order_result(account=account, ticker="AAPL", side="buy",
                                submitted=False, detail="risk gate rejected"),
            events.review_queued(account=account, review_id=12, ticker="AAPL",
                                 action="sell 50%", strategy_id="s1",
                                 recommendation="execute", trigger_price="$204.50",
                                 est_shares="2.44"),
            events.review_queued(account=account, review_id=13, ticker="",
                                 action="Reset ledger", strategy_id="",
                                 kind="shadow_edit"),
            events.daily_digest(account=account, triggers=2, trades=1, pending=3,
                                llm_cost="$0.42"),
            events.daily_report(account=account, date="2026-08-21",
                                summary="s", body="b"),
        ]:
            for text in (subject, body):
                assert_english_only(text)
