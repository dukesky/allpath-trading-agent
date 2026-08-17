import threading
import time

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.strategy.model import RuleState
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
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


# --- status lifecycle (Activate / Pause / Resume) ---------------------------

def _write_strategy_with_status(client, strategy_id, status, *, extra=""):
    store = client.app.state.holder.get().strategies
    text = (
        f'name: "{strategy_id}"\n'
        f"status: {status}\n"
        "position: {ticker: MSFT, target_weight: 10%}\n"
        "rules:\n"
        '  - {id: r1, type: soft, condition: "price < 100", action: "sell 10%"}\n'
        f"{extra}"
    )
    (store.directory / f"{strategy_id}.yaml").write_text(text)
    return store.directory / f"{strategy_id}.yaml"


def test_status_route_activates_a_draft_strategy(client):
    _write_strategy_with_status(client, "draftstrat", "draft")
    store = client.app.state.holder.get().strategies
    r = client.post("/strategies/draftstrat/status", data={"to": "active"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/strategies/draftstrat"
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "draftstrat")
    assert doc.status.value == "active"
    versions = store.versions("draftstrat")
    assert versions[0]["reason"] == "status changed to active via web"


def test_status_route_pauses_an_active_strategy(client):
    store = client.app.state.holder.get().strategies
    r = client.post("/strategies/semis/status", data={"to": "paused"},
                    follow_redirects=False)
    assert r.status_code == 303
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.status.value == "paused"
    versions = store.versions("semis")
    assert versions[0]["reason"] == "status changed to paused via web"


def test_status_route_resumes_a_paused_strategy(client):
    _write_strategy_with_status(client, "pausedstrat", "paused")
    store = client.app.state.holder.get().strategies
    r = client.post("/strategies/pausedstrat/status", data={"to": "active"},
                    follow_redirects=False)
    assert r.status_code == 303
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "pausedstrat")
    assert doc.status.value == "active"
    versions = store.versions("pausedstrat")
    assert versions[0]["reason"] == "status changed to active via web"


def test_status_route_preserves_rules_byte_for_byte_apart_from_status(client):
    path = _write_strategy_with_status(client, "draftstrat", "draft")
    client.post("/strategies/draftstrat/status", data={"to": "active"})
    text = path.read_text()
    assert "condition: price < 100" in text
    assert "action: sell 10%" in text


def test_status_route_does_not_persist_runtime_rule_state(client):
    store = client.app.state.holder.get().strategies
    store.set_rule_state("semis", "r1", RuleState.TRIGGERED)
    client.post("/strategies/semis/status", data={"to": "paused"})
    path = store.directory / "semis.yaml"
    assert "triggered" not in path.read_text()
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.rules[0].state.value == "triggered"


def test_status_route_rejects_disallowed_transition_and_leaves_file_untouched(client):
    store = client.app.state.holder.get().strategies
    path = store.directory / "semis.yaml"
    before = path.read_text()
    r = client.post("/strategies/semis/status", data={"to": "draft"})
    assert r.status_code == 200
    assert "not processed" in r.text.lower()
    assert path.read_text() == before
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.status.value == "active"


def test_status_route_rejects_transition_from_archived(client):
    path = _write_strategy_with_status(client, "archivedstrat", "archived")
    before = path.read_text()
    r = client.post("/strategies/archivedstrat/status", data={"to": "active"})
    assert r.status_code == 200
    assert "not processed" in r.text.lower()
    assert path.read_text() == before


def test_status_route_rejects_invalid_target_value(client):
    store = client.app.state.holder.get().strategies
    path = store.directory / "semis.yaml"
    before = path.read_text()
    r = client.post("/strategies/semis/status", data={"to": "bogus"})
    assert r.status_code == 200
    assert "not processed" in r.text.lower()
    assert path.read_text() == before


def test_status_route_unknown_strategy_404s(client):
    r = client.post("/strategies/nope/status", data={"to": "active"})
    assert r.status_code == 404


def test_status_route_invalid_id_is_refused(client):
    r = client.post("/strategies/..%2f..%2fetc%2fpasswd/status",
                    data={"to": "active"}, follow_redirects=False)
    assert r.status_code in (400, 404)


def test_detail_page_shows_activate_button_for_draft(client):
    _write_strategy_with_status(client, "draftstrat", "draft")
    body = client.get("/strategies/draftstrat").text
    assert 'action="/strategies/draftstrat/status"' in body
    assert '<button type="submit">Activate</button>' in body


def test_detail_page_shows_pause_button_for_active(client):
    body = client.get("/strategies/semis").text
    assert 'action="/strategies/semis/status"' in body
    assert '<button type="submit">Pause</button>' in body


def test_detail_page_shows_resume_button_for_paused(client):
    _write_strategy_with_status(client, "pausedstrat", "paused")
    body = client.get("/strategies/pausedstrat").text
    assert '<button type="submit">Resume</button>' in body


def test_detail_page_shows_no_lifecycle_button_for_archived(client):
    _write_strategy_with_status(client, "archivedstrat", "archived")
    body = client.get("/strategies/archivedstrat").text
    assert '/status"' not in body


def test_detail_page_activate_button_has_confirmation(client):
    _write_strategy_with_status(client, "draftstrat", "draft")
    body = client.get("/strategies/draftstrat").text
    assert "onsubmit=" in body
    assert "confirm(" in body
    assert "sentinel will evaluate" in body.lower()
    # M5: the interval is user-editable (sentinel_interval_minutes) -- the
    # dialog must not hardcode "hourly"/"every hour".
    assert "every hour" not in body.lower()
    assert "hourly" not in body.lower()


def test_detail_page_activate_button_confirmation_warns_about_pending_revisions(client):
    # M7: activating/resuming/pausing rewrites the strategy YAML, so any
    # pending revision proposal for it (diffed against the pre-change file)
    # would no longer apply cleanly -- the confirm dialog must say so.
    _write_strategy_with_status(client, "draftstrat", "draft")
    body = client.get("/strategies/draftstrat").text
    assert "pending revision proposal for this strategy will need re-proposing" in body


def test_detail_page_pause_button_confirmation_warns_about_pending_revisions(client):
    body = client.get("/strategies/semis").text
    assert "pending revision proposal for this strategy will need re-proposing" in body


def test_detail_page_resume_button_confirmation_warns_about_pending_revisions(client):
    _write_strategy_with_status(client, "pausedstrat", "paused")
    body = client.get("/strategies/pausedstrat").text
    assert "pending revision proposal for this strategy will need re-proposing" in body


def test_status_route_missing_to_field_is_not_processed_not_a_bare_422(client):
    # M8: `to` used to be a required Form field -- posting without it hit
    # FastAPI's own validation and returned a bare 422 JSON body, dropping
    # the user out of the app chrome entirely. It must now behave like any
    # other bad input to this route: a 200 back on the strategy's own page
    # with a "not processed" message.
    r = client.post("/strategies/semis/status", data={})
    assert r.status_code == 200
    assert "not processed" in r.text.lower()
    assert '{"detail"' not in r.text
    doc = next(d for d in client.app.state.holder.get().strategies
               .load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.status.value == "active"  # unchanged


# --- draft/paused not-monitored warnings ------------------------------------

def test_list_shows_not_monitored_badge_for_draft(client):
    _write_strategy_with_status(client, "draftstrat", "draft")
    body = client.get("/strategies").text
    assert "not monitored" in body


def test_list_shows_not_monitored_badge_for_paused(client):
    _write_strategy_with_status(client, "pausedstrat", "paused")
    body = client.get("/strategies").text
    assert "not monitored" in body


def test_list_omits_not_monitored_badge_for_active(client):
    body = client.get("/strategies").text
    assert "not monitored" not in body


def test_list_shows_hint_line_for_draft(client):
    _write_strategy_with_status(client, "draftstrat", "draft")
    body = client.get("/strategies").text
    assert ("Draft strategies are not evaluated by the sentinel"
            in body)


def test_list_shows_hint_line_for_paused(client):
    _write_strategy_with_status(client, "pausedstrat", "paused")
    body = client.get("/strategies").text
    assert ("Paused strategies are not evaluated by the sentinel"
            in body)


def test_detail_shows_not_monitored_badge_for_draft(client):
    _write_strategy_with_status(client, "draftstrat", "draft")
    body = client.get("/strategies/draftstrat").text
    assert "not monitored" in body


def test_detail_omits_not_monitored_badge_for_active(client):
    body = client.get("/strategies/semis").text
    assert "not monitored" not in body


def test_strategies_page_shows_status_legend(client):
    body = client.get("/strategies").text
    assert "draft: not yet live" in body
    assert "active: evaluated on every sentinel pass during market hours" in body
    assert "paused: temporarily off" in body
    assert "archived: kept for reference" in body


# --- horizon / bias chips ----------------------------------------------------

def _write_strategy_with_horizon_bias(client, strategy_id, *, horizon=None, bias=None,
                                      thesis=None):
    store = client.app.state.holder.get().strategies
    lines = [
        f'name: "{strategy_id}"',
        "status: active",
        "position: {ticker: MSFT, target_weight: 10%}",
        "rules: []",
    ]
    if horizon is not None:
        lines.append(f"horizon: {horizon}")
    if bias is not None:
        lines.append(f"bias: {bias}")
    if thesis is not None:
        lines.append(f'thesis: "{thesis}"')
    (store.directory / f"{strategy_id}.yaml").write_text("\n".join(lines) + "\n")


def test_list_shows_horizon_chip_when_present(client):
    _write_strategy_with_horizon_bias(client, "longstrat", horizon="long")
    body = client.get("/strategies").text
    assert "Long-term" in body


def test_list_omits_horizon_chip_when_absent(client):
    body = client.get("/strategies").text
    assert "Long-term" not in body
    assert "Medium-term" not in body
    assert "Swing" not in body


def test_list_shows_bullish_bias_chip_with_up_class(client):
    _write_strategy_with_horizon_bias(client, "bullstrat", bias="bullish")
    body = client.get("/strategies").text
    assert 'class="bias-chip bullish"' in body


def test_list_shows_bearish_bias_chip_with_down_class(client):
    _write_strategy_with_horizon_bias(client, "bearstrat", bias="bearish")
    body = client.get("/strategies").text
    assert 'class="bias-chip bearish"' in body


def test_list_shows_neutral_bias_chip_with_neutral_class(client):
    _write_strategy_with_horizon_bias(client, "neutralstrat", bias="neutral")
    body = client.get("/strategies").text
    assert 'class="bias-chip neutral"' in body


def test_list_omits_bias_chip_when_absent(client):
    body = client.get("/strategies").text
    assert "bias-chip" not in body


def test_list_shows_escaped_thesis_excerpt(client):
    _write_strategy_with_horizon_bias(client, "thesisstrat",
                                      thesis="Bullish on <b>services</b> growth.")
    body = client.get("/strategies").text
    assert "&lt;b&gt;services&lt;/b&gt;" in body
    assert "<b>services</b>" not in body


def test_list_omits_thesis_line_when_absent(client):
    body = client.get("/strategies").text  # STRAT fixture has no thesis
    # No stray empty thesis paragraph -- nothing to assert positively here
    # beyond the page still rendering cleanly.
    assert "Semis core" in body


def test_strategies_page_is_english_only_with_new_content(client):
    _write_strategy_with_status(client, "draftstrat", "draft")
    _write_strategy_with_horizon_bias(client, "bullstrat", horizon="long", bias="bullish",
                                      thesis="Bullish on growth.")
    assert_english_only(client.get("/strategies").text)
    assert_english_only(client.get("/strategies/draftstrat").text)
    assert_english_only(client.get("/strategies/bullstrat").text)


# --- M2/M3: /strategies shares dashboard's quote budget & cache -------------

def test_strategies_page_renders_within_quote_budget_when_source_hangs(client, monkeypatch):
    # M2: strategies.py used to import QUOTES_BUDGET_SECONDS by value at
    # import time, so shrinking dashboard_route.QUOTES_BUDGET_SECONDS in a
    # test had no effect on this page's own quote loop. This proves the
    # budget is read from the module at call time and is actually enforced
    # here too, not just on the dashboard: N strategies against a hanging
    # data source cost N * BROKER_TIMEOUT_SECONDS without the shared budget;
    # with it, total time stays close to one BROKER_TIMEOUT_SECONDS, and the
    # strategies whose quote lookup never started in time degrade to "—"
    # instead of hanging the page.
    store = client.app.state.holder.get().strategies
    for i in range(4):
        (store.directory / f"extra{i}.yaml").write_text(f"""
name: "Extra {i}"
status: active
position: {{ticker: TICK{i}, target_weight: 5%}}
rules: []
""")
    monkeypatch.setattr(dashboard_route, "BROKER_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(dashboard_route, "QUOTES_BUDGET_SECONDS", 0.05)
    data = client.app.state.holder.get().data
    release = threading.Event()

    def hang(ticker):
        release.wait(timeout=5)
        raise RuntimeError("never resolves in time")

    monkeypatch.setattr(data, "get_quote", hang)
    dashboard_route._quote_cache.clear()

    start = time.monotonic()
    r = client.get("/strategies")
    elapsed = time.monotonic() - start

    assert r.status_code == 200
    # 5 strategies total * 0.3s BROKER_TIMEOUT_SECONDS would be 1.5s without
    # the shared budget; the budget bounds this to roughly one timeout.
    assert elapsed < 1.0
    # A quote that never came back renders as "—" (format.money's failure
    # copy), never as a stuck page or a raised error.
    assert "—" in r.text
    release.set()  # let the background call finish so it doesn't linger


# --- Task 4: strategy detail page points at a pending proposal, source-aware

def test_detail_page_shows_pending_chat_draft_line(client):
    queue = client.app.state.holder.get().queue
    text = (client.app.state.holder.get().strategies.directory / "semis.yaml").read_text()
    rid = queue.add_strategy_revision(
        strategy_id="semis", ticker="AAPL", old_yaml=text, new_yaml=text,
        diff="d", rationale="tighten stop", source="chat")
    body = client.get("/strategies/semis").text
    assert f"A chat draft (#{rid}) is awaiting your approval" in body
    assert 'href="/reviews"' in body


def test_detail_page_shows_pending_reflection_proposal_line(client):
    queue = client.app.state.holder.get().queue
    text = (client.app.state.holder.get().strategies.directory / "semis.yaml").read_text()
    rid = queue.add_strategy_revision(
        strategy_id="semis", ticker="AAPL", old_yaml=text, new_yaml=text,
        diff="d", rationale="tighten stop")  # default source="reflection"
    body = client.get("/strategies/semis").text
    assert f"A reflection proposal (#{rid}) is awaiting your approval" in body
    assert 'href="/reviews"' in body


def test_detail_page_omits_pending_proposal_line_when_none(client):
    body = client.get("/strategies/semis").text
    assert "is awaiting your approval" not in body


def test_detail_page_omits_pending_proposal_line_for_a_different_strategy(client):
    _write_strategy_with_status(client, "other", "active")
    queue = client.app.state.holder.get().queue
    text = (client.app.state.holder.get().strategies.directory / "other.yaml").read_text()
    queue.add_strategy_revision(
        strategy_id="other", ticker="AAPL", old_yaml=text, new_yaml=text,
        diff="d", rationale="tighten stop", source="chat")
    body = client.get("/strategies/semis").text
    assert "is awaiting your approval" not in body


def test_detail_page_pending_proposal_line_is_english_only(client):
    queue = client.app.state.holder.get().queue
    text = (client.app.state.holder.get().strategies.directory / "semis.yaml").read_text()
    queue.add_strategy_revision(
        strategy_id="semis", ticker="AAPL", old_yaml=text, new_yaml=text,
        diff="d", rationale="tighten stop", source="chat")
    assert_english_only(client.get("/strategies/semis").text)
