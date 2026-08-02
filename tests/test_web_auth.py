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
    # Task 6 added the dashboard at "/", so an authenticated GET there can
    # now be held to the real bar: 200, not just "didn't redirect".
    r2 = client.get("/", follow_redirects=False)
    assert r2.status_code == 200


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


def test_non_ascii_login_token_is_rejected_not_500(client):
    # hmac.compare_digest raises TypeError on non-ASCII `str` operands.
    # A pasted token containing a smart quote or accent must fail closed
    # with the normal "invalid token" page, not a 500.
    r = client.post("/login", data={"token": "café"}, follow_redirects=False)
    assert r.status_code == 401
    assert "allpath_session" not in r.cookies


def test_non_ascii_session_cookie_is_rejected_not_500(client):
    # Same failure mode as above, on the cookie side of `_authorized`: a
    # request carrying a non-ASCII cookie value must redirect to /login,
    # not raise. httpx's own Cookies jar refuses to build a `str` header
    # containing non-ASCII characters (it round-trips through ascii
    # encoding), so `client.cookies.set(...)` can't reproduce the real
    # wire condition -- passing the raw header as latin-1-encoded `bytes`
    # bypasses that and reproduces what Starlette actually decodes off
    # the wire (latin-1 bytes -> a non-ASCII `str`), matching how the
    # reviewer triggered this against the real app.
    r = client.get("/", follow_redirects=False,
                    headers={"cookie": "allpath_session=caf\xe9".encode("latin-1")})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# -- The absent/present-Origin decision (see auth.py's comment in the
# middleware): both must be reachable without ever hitting the 403 branch,
# otherwise a future change that flips `if origin is not None` would pass
# the rest of this suite while breaking every non-JS form post. --


def test_absent_origin_post_is_allowed(client):
    client.post("/login", data={"token": "secret"})
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.status_code != 403


def test_same_origin_post_is_allowed(client):
    client.post("/login", data={"token": "secret"})
    r = client.post("/logout", headers={"origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.status_code != 403


def test_healthz_with_trailing_slash_is_still_public(client):
    # A health probe configured with a trailing slash must still get the
    # health response, not a 303 to /login.
    r = client.get("/healthz/", follow_redirects=False)
    assert r.status_code != 303
