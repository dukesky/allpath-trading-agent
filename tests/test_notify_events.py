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
