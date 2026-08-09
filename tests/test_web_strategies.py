import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.strategy.model import RuleState
from allpath_trade.web.app import create_app
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker

STRAT = """
name: "Semis core"
status: active
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "semis.yaml").write_text(STRAT)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def test_list_shows_each_strategy(client):
    assert "Semis core" in client.get("/strategies").text


def test_detail_shows_yaml_and_rules(client):
    body = client.get("/strategies/semis").text
    assert "target_weight" in body
    assert "r1" in body


def test_strategies_pages_are_english_only(client):
    assert_english_only(client.get("/strategies").text)
    assert_english_only(client.get("/strategies/semis").text)


def test_unknown_strategy_returns_404(client):
    assert client.get("/strategies/nope").status_code == 404


def test_unknown_strategy_404_stays_inside_the_app_chrome(client):
    # C1: HTTPException(404) used to drop straight out of the templates
    # into a bare `{"detail": "not found"}` JSON body -- the only one of six
    # pages that did. It must render the same nav/layout every other error
    # on this site does, just at a 404 status.
    r = client.get("/strategies/nope")
    assert r.status_code == 404
    assert "allpath trade" in r.text.lower()  # base.html's nav brand
    assert "<nav>" in r.text
    assert "not found" in r.text.lower()
    assert '{"detail"' not in r.text


def test_path_traversal_is_refused(client):
    assert client.get("/strategies/..%2f..%2fetc%2fpasswd").status_code in (400, 404)


def test_rearm_resets_a_triggered_rule(client):
    store = client.app.state.holder.get().strategies
    store.set_rule_state("semis", "r1", RuleState.TRIGGERED)
    client.post("/strategies/semis/rules/r1/rearm", follow_redirects=False)
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.rules[0].state.value == "armed"


def test_rearm_path_traversal_is_refused(client):
    r = client.post("/strategies/..%2f..%2fetc%2fpasswd/rules/r1/rearm",
                    follow_redirects=False)
    assert r.status_code in (400, 404)


def test_detail_of_unparseable_yaml_returns_404_not_500(client):
    (client.app.state.holder.get().strategies.directory / "broken.yaml").write_text(
        "not: [valid, yaml structure for a strategy")
    r = client.get("/strategies/broken")
    assert r.status_code == 404


def test_rearm_of_well_formed_but_nonexistent_strategy_is_not_processed(client):
    store = client.app.state.holder.get().strategies
    r = client.post("/strategies/ghost/rules/r1/rearm")
    assert r.status_code == 200
    assert "not processed" in r.text.lower()
    assert "ghost" in r.text
    # No orphan row was written for a strategy that was never loaded.
    rows = store._conn.execute(
        "SELECT * FROM rule_states WHERE strategy_id = ?", ("ghost",)).fetchall()
    assert rows == []


def test_detail_page_caps_rendered_version_history(client):
    store = client.app.state.holder.get().strategies
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    for i in range(1, 26):
        store.snapshot_version(doc.model_copy(update={"version": i}), reason=f"edit-{i}")
    # The store itself keeps all 25 snapshots -- other callers (e.g. the
    # action-tool tests) rely on getting everything back.
    assert len(store.versions("semis")) == 25
    body = client.get("/strategies/semis").text
    assert body.count("edit-") <= 20
    assert "edit-25</td>" in body  # most recent kept
    assert "edit-5</td>" not in body  # oldest trimmed


def test_strategies_list_shows_status_and_lifecycle_chips(client):
    c = client.app.state.holder.get()
    c.strategies.set_rule_state("semis", "r1", RuleState.TRIGGERED)
    c.queue.add(strategy_id="semis", rule_id="r1", ticker="AAPL", rule_type="hard",
                condition="price < 100", action="sell all", snapshot={}, intent=None)
    body = client.get("/strategies").text
    assert "active" in body
    assert "1 triggered" in body
    assert "pending review" in body


def test_strategies_list_omits_chips_when_nothing_is_running(client):
    body = client.get("/strategies").text
    assert "triggered" not in body
    assert "pending review" not in body


def test_notify_email_toggle_flips_field_and_records_a_version(client):
    store = client.app.state.holder.get().strategies
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.notify_email is True

    r = client.post("/strategies/semis/notify-email", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/strategies/semis"

    doc2 = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc2.notify_email is False
    versions = store.versions("semis")
    assert versions[0]["reason"] == "notify_email toggled via web"

    # Toggling again flips it back -- and it's a genuine YAML rewrite each
    # time, not a string edit (rule fields must still round-trip untouched).
    client.post("/strategies/semis/notify-email")
    doc3 = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc3.notify_email is True
    assert doc3.rules[0].condition == "price < 100"


def test_notify_email_toggle_does_not_persist_runtime_rule_state(client):
    # rule state lives in SQLite, not the YAML -- writing the file back
    # through the parse/dump round trip must not bake a triggered rule's
    # runtime state into the source of truth.
    store = client.app.state.holder.get().strategies
    store.set_rule_state("semis", "r1", RuleState.TRIGGERED)
    client.post("/strategies/semis/notify-email")
    path = store.directory / "semis.yaml"
    assert "triggered" not in path.read_text()
    # the SQLite-backed state is unaffected by the toggle
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.rules[0].state.value == "triggered"


def test_notify_email_toggle_unknown_strategy_404s(client):
    r = client.post("/strategies/nope/notify-email")
    assert r.status_code == 404


def test_notify_email_toggle_path_traversal_is_refused(client):
    r = client.post("/strategies/..%2f..%2fetc%2fpasswd/notify-email",
                    follow_redirects=False)
    assert r.status_code in (400, 404)


def test_detail_page_shows_notify_email_toggle_control(client):
    body = client.get("/strategies/semis").text
    assert 'action="/strategies/semis/notify-email"' in body


def test_rearm_of_bogus_rule_on_a_real_strategy_is_not_processed(client):
    store = client.app.state.holder.get().strategies
    r = client.post("/strategies/semis/rules/no-such-rule/rearm")
    assert r.status_code == 200
    assert "not processed" in r.text.lower()
    assert "no-such-rule" in r.text
    # The real strategy's real rule is untouched, and no orphan row for the
    # bogus rule_id was written.
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.rules[0].state.value == "armed"
    rows = store._conn.execute(
        "SELECT * FROM rule_states WHERE strategy_id = ? AND rule_id = ?",
        ("semis", "no-such-rule")).fetchall()
    assert rows == []


def test_status_chip_and_alert_chip_use_different_classes(client):
    # Finding 1: lifecycle status ("active") must not render in the same
    # amber .badge used for things that need attention (N triggered,
    # pending review) -- it should carry the neutral .chip class instead.
    c = client.app.state.holder.get()
    c.strategies.set_rule_state("semis", "r1", RuleState.TRIGGERED)
    c.queue.add(strategy_id="semis", rule_id="r1", ticker="AAPL", rule_type="hard",
                condition="price < 100", action="sell all", snapshot={}, intent=None)

    list_body = client.get("/strategies").text
    assert '<span class="chip">active</span>' in list_body
    assert '<span class="badge">1 triggered</span>' in list_body
    assert '<span class="badge">pending review</span>' in list_body
    assert '<span class="badge">active</span>' not in list_body

    detail_body = client.get("/strategies/semis").text
    assert '<span class="chip">active</span>' in detail_body
    assert '<span class="badge">1 triggered</span>' in detail_body
    assert '<span class="badge">active</span>' not in detail_body


def _write_strategy(client, strategy_id, *, authorization, notify_email):
    store = client.app.state.holder.get().strategies
    text = (
        f'name: "{strategy_id}"\n'
        "status: active\n"
        f"authorization: {authorization}\n"
        f"notify_email: {str(notify_email).lower()}\n"
        "position: {ticker: MSFT, target_weight: 10%}\n"
        "rules:\n"
        '  - {id: r1, type: soft, condition: "price < 100", action: "sell 10%"}\n'
    )
    (store.directory / f"{strategy_id}.yaml").write_text(text)


def test_notify_only_strategy_with_email_off_shows_warning(client):
    _write_strategy(client, "silent", authorization="notify", notify_email=False)
    body = client.get("/strategies/silent").text
    assert ("This strategy is notify-only: with notifications off, a "
            "trigger will only be visible on this page and the "
            "dashboard.") in body


def test_notify_only_strategy_with_email_on_shows_no_warning(client):
    _write_strategy(client, "loud", authorization="notify", notify_email=True)
    body = client.get("/strategies/loud").text
    assert "notify-only" not in body


def test_non_notify_strategy_with_email_off_shows_no_warning(client):
    _write_strategy(client, "confirmed", authorization="confirm", notify_email=False)
    body = client.get("/strategies/confirmed").text
    assert "notify-only" not in body
