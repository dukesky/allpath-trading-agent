import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        yield c


def test_anonymous_request_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_with_the_wrong_token_is_rejected(client):
    r = client.post("/login", data={"token": "nope"}, follow_redirects=False)
    assert r.status_code == 401
    assert "allpath_session" not in r.cookies


def test_login_then_browse(client):
    r = client.post("/login", data={"token": "secret"}, follow_redirects=False)
    assert r.status_code == 303
    # Task 5 owns only /login and /logout -- "/" itself isn't a route yet
    # (Task 6 adds the dashboard), so a 200 there isn't available to assert
    # on. What this test can and must prove is that the auth gate itself
    # passed: an authenticated GET to a protected path no longer gets
    # redirected to /login (a 404 here means routing, not auth, rejected
    # it).
    r2 = client.get("/", follow_redirects=False)
    assert r2.status_code != 303


def test_cross_origin_post_is_rejected(client):
    # Brief targets /reviews/1/reject, which doesn't exist until Task 7.
    # /logout is the only state-changing route available now; Task 7
    # re-points this at a real reviews route once it lands.
    client.post("/login", data={"token": "secret"})
    r = client.post("/logout", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_static_assets_need_no_auth(client):
    assert client.get("/static/app.css").status_code in (200, 404)


# -- Additional tests for the security properties the task exists to
# deliver, beyond the brief's baseline set. --


def test_empty_web_token_never_authenticates(tmp_path, monkeypatch):
    """A blank WEB_TOKEN must never grant access, even if a client submits
    a blank token to match it -- otherwise an unconfigured server (the
    out-of-the-box state before `ensure_token` first runs) would be wide
    open on the LAN."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        r = c.post("/login", data={"token": ""}, follow_redirects=False)
        assert r.status_code == 401
        assert "allpath_session" not in c.cookies


def test_session_cookie_is_httponly_and_samesite_strict(client):
    r = client.post("/login", data={"token": "secret"}, follow_redirects=False)
    set_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()


def test_forged_cookie_value_is_rejected(client):
    # A cookie that merely exists but doesn't match the real token must not
    # authenticate -- guards against a non-constant-time or truthiness-only
    # comparison letting any non-empty cookie through.
    client.cookies.set("allpath_session", "not-the-real-token")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
