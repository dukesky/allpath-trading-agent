from allpath_trade.notify import events
from tests.helpers import assert_english_only


def test_bodies_contain_no_links_or_html():
    subject, body = events.review_queued(
        review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        recommendation="execute")
    assert "http" not in body.lower()
    assert "<" not in body
    assert "12" in body and "AAPL" in subject


def test_bodies_are_english_only():
    for subject, body in [
        events.rule_triggered(strategy_id="s", rule_id="r", ticker="AAPL",
                              condition="price < 100", disposition="queued"),
        events.order_result(ticker="AAPL", side="buy", submitted=True,
                            detail="filled 3 @ 220.15"),
        events.daily_digest(triggers=2, trades=1, pending=3),
    ]:
        for text in (subject, body):
            assert_english_only(text)


# -- Approve-by-link (Part A): the footer must stay truthful either way --
# it's appended to every notification, whether or not that particular one
# ends up carrying a link.


def test_footer_no_longer_claims_no_links_by_design():
    _, body = events.rule_triggered(strategy_id="s", rule_id="r", ticker="AAPL",
                                    condition="price < 100", disposition="queued")
    assert "no links by design" not in body
    assert "never carries your dashboard access token" in body


def test_review_queued_without_approve_url_carries_no_link():
    _subject, body = events.review_queued(
        review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1")
    assert "http" not in body.lower()
    assert "Review & approve" not in body


def test_review_queued_with_approve_url_includes_it_once():
    _subject, body = events.review_queued(
        review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        approve_url="http://192.168.1.20:8791/a/12?k=abc123")
    assert "Review & approve: http://192.168.1.20:8791/a/12?k=abc123" in body
    assert body.count("http://192.168.1.20:8791/a/12?k=abc123") == 1


def test_review_queued_price_context_is_included_when_present():
    _subject, body = events.review_queued(
        review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
        trigger_price="$204.50", est_shares="2.44")
    assert "Price at trigger: $204.50" in body
    assert "Est. size: ~2.44 shares at that price" in body


def test_review_queued_price_context_is_omitted_when_absent():
    _subject, body = events.review_queued(
        review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1")
    assert "Price at trigger" not in body
    assert "Est. size" not in body


def test_review_queued_with_approve_url_is_english_only():
    subject, body = events.review_queued(
        review_id=12, ticker="AAPL", action="sell 50%", strategy_id="s1",
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
