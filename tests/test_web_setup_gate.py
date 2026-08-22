"""setup-wizard T2: the gate wired into the real app -- an unconfigured
install's browsing is bounced to the wizard, everything that has to keep
working before setup is finished still works, and a user who dismissed the
wizard gets the pages back with a banner instead.

The `/setup` page itself is Task 3, so the redirect target 404s for now;
these tests assert the 302 and its `Location`, never the wizard's body."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from allpath_trade.web.setup_status import SETUP_DISMISSED_KEY
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker

CONFIGURED = {"openrouter_api_key": "k", "alpaca_api_key": "a",
              "alpaca_secret_key": "s"}


def _client(tmp_path, **settings_kwargs) -> TestClient:
    (tmp_path / "strategies").mkdir(exist_ok=True)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        **settings_kwargs)
    return TestClient(create_app(settings, broker=FakeBroker()))


def _dismiss(client: TestClient) -> None:
    client.app.state.holder.get().app_state.set(SETUP_DISMISSED_KEY, "1")


@pytest.fixture
def unconfigured(tmp_path, monkeypatch):
    """Logged in, but with no LLM key and no Alpaca keys -- the state a
    brand-new install is in the first time it is opened in a browser."""
    monkeypatch.chdir(tmp_path)
    with _client(tmp_path) as c:
        c.post("/login", data={"token": "secret"})
        yield c


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with _client(tmp_path, **CONFIGURED) as c:
        c.post("/login", data={"token": "secret"})
        yield c


# -- the redirect itself --


@pytest.mark.parametrize("path", ["/", "/chat", "/reviews", "/strategies",
                                  "/memory", "/reports", "/settings"])
def test_every_page_redirects_to_setup_while_keys_are_missing(unconfigured, path):
    r = unconfigured.get(path, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_nothing_is_redirected_once_both_keys_are_present(configured):
    assert configured.get("/", follow_redirects=False).status_code == 200


def test_an_htmx_get_gets_hx_redirect_instead_of_a_swappable_body(unconfigured):
    """htmx follows a 302 inside the same AJAX exchange and swaps whatever
    comes back into the triggering element -- so a bare redirect here would
    splice the wizard (or, until Task 3 lands it, a 404 page) into some div
    with nothing to show the user why. `HX-Redirect` makes the browser
    navigate for real instead. No template issues an `hx-get` today; this
    holds the contract for the first one that does, and matches what the
    login bounce has always done (see web/auth.py::_redirect)."""
    r = unconfigured.get("/", headers={"HX-Request": "true"},
                         follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/setup"
    assert r.text == ""  # nothing swappable, even if a handler ignored it


def test_an_htmx_get_is_not_redirected_once_configured(configured):
    r = configured.get("/", headers={"HX-Request": "true"},
                       follow_redirects=False)
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers


def test_a_post_is_never_redirected(unconfigured):
    """A 302 on a POST is re-issued as a GET with the body dropped -- the
    settings save that enters the missing keys would be the first casualty.
    /chat/send stands in for any POST here; it is expected to fail on its
    own terms (no LLM key), just not by being bounced to the wizard."""
    r = unconfigured.post("/chat/send", data={"message": "hi"},
                          follow_redirects=False)
    assert r.status_code != 302
    assert r.headers.get("location") != "/setup"


def test_approval_links_still_work_without_finishing_setup(unconfigured):
    """`/a/*` is reachable without a session cookie by design (a tap from a
    notification); the setup gate must not undo that. The token below is
    invalid, so the route answers on its own terms -- what matters is that
    it is not a bounce to the wizard."""
    r = unconfigured.get("/a/1", follow_redirects=False)
    assert r.headers.get("location") != "/setup"


@pytest.mark.parametrize("path", ["/login", "/healthz", "/static/app.css"])
def test_the_pre_login_surfaces_stay_reachable(unconfigured, path):
    r = unconfigured.get(path, follow_redirects=False)
    assert r.headers.get("location") != "/setup"


def test_an_anonymous_request_still_goes_to_login_not_setup(tmp_path, monkeypatch):
    """The gate sits behind the token check, so a stranger on the LAN
    learns nothing about this install's setup state."""
    monkeypatch.chdir(tmp_path)
    with _client(tmp_path) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_dismissing_setup_gives_the_pages_back(unconfigured):
    _dismiss(unconfigured)
    assert unconfigured.get("/", follow_redirects=False).status_code == 200


def test_the_gate_follows_a_settings_change_without_a_restart(configured, tmp_path):
    """`holder.rebuild()` is what the settings save calls; the gate reads
    the live Settings through it, so removing the keys re-arms the gate
    (and, in the direction that actually matters, entering them lifts it)."""
    stripped = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    configured.app.state.holder.rebuild(stripped)
    r = configured.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


# -- the banner --


BANNER = "Setup incomplete"


def test_the_banner_names_what_is_missing_after_dismissal(unconfigured):
    _dismiss(unconfigured)
    body = unconfigured.get("/").text
    assert BANNER in body
    assert "LLM key, Alpaca keys missing" in body
    assert 'href="/setup"' in body
    assert_english_only(body)


def test_the_banner_lists_only_the_half_that_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with _client(tmp_path, openrouter_api_key="k") as c:
        c.post("/login", data={"token": "secret"})
        _dismiss(c)
        body = c.get("/").text
        assert "Alpaca keys missing" in body
        assert "LLM key" not in body
        assert_english_only(body)


def test_no_banner_once_nothing_is_missing(configured):
    assert BANNER not in configured.get("/").text


@pytest.mark.parametrize("path", ["/chat", "/reviews", "/strategies",
                                  "/memory", "/reports", "/settings"])
def test_the_banner_is_on_every_page_not_just_the_dashboard(unconfigured, path):
    _dismiss(unconfigured)
    body = unconfigured.get(path).text
    assert BANNER in body
    assert_english_only(body)


def _nav_context_for(app, path: str) -> dict:
    """`nav_context` straight from a synthetic request. The wizard's own
    pages are Task 3, so there is no `/setup*` route to GET yet -- this is
    how the "no banner on the wizard itself" rule gets held to now."""
    from starlette.requests import Request

    from allpath_trade.web.routes.dashboard import nav_context

    return nav_context(Request({"type": "http", "method": "GET", "path": path,
                                "query_string": b"", "headers": [], "app": app}))


@pytest.mark.parametrize("path", ["/setup", "/setup/", "/setup/keys"])
def test_the_banner_is_suppressed_on_the_wizard_itself(unconfigured, path):
    context = _nav_context_for(unconfigured.app, path)
    assert context["setup_missing"] == ["LLM key", "Alpaca keys"]
    assert context["setup_banner"] is False


def test_nav_context_asks_for_the_banner_on_an_ordinary_page(unconfigured):
    assert _nav_context_for(unconfigured.app, "/")["setup_banner"] is True


def test_nav_context_reports_nothing_missing_once_configured(configured):
    context = _nav_context_for(configured.app, "/")
    assert context["setup_missing"] == []
    assert context["setup_banner"] is False
