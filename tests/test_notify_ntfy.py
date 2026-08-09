from __future__ import annotations

import urllib.error

from allpath_trade.config import Settings
from allpath_trade.notify.email import build_notifier
from allpath_trade.notify.ntfy import NtfyNotifier


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


def test_successful_post_returns_true_and_sends_title_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["title"] = req.get_header("Title")
        captured["data"] = req.data
        captured["method"] = req.get_method()
        captured["timeout"] = timeout
        return _FakeResponse(200)

    # Never hits the network -- urlopen itself is replaced.
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    n = NtfyNotifier("https://ntfy.sh/my-topic")
    assert n.send("Trigger: AAPL", "details") is True
    assert captured["url"] == "https://ntfy.sh/my-topic"
    assert captured["title"] == "Trigger: AAPL"
    assert captured["data"] == b"details"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 10


def test_non_2xx_status_returns_false_and_prints_one_stderr_line(monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResponse(500))
    n = NtfyNotifier("https://ntfy.sh/my-topic")
    assert n.send("s", "b") is False
    err_lines = capsys.readouterr().err.strip().splitlines()
    assert len(err_lines) == 1
    assert "notify" in err_lines[0]


def test_connection_error_returns_false_and_never_raises(monkeypatch, capsys):
    def raise_error(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", raise_error)
    n = NtfyNotifier("https://ntfy.sh/my-topic")
    assert n.send("s", "b") is False
    err_lines = capsys.readouterr().err.strip().splitlines()
    assert len(err_lines) == 1
    assert "notify" in err_lines[0]


def test_http_error_returns_false_and_never_raises(monkeypatch, capsys):
    def raise_http_error(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", raise_http_error)
    n = NtfyNotifier("https://ntfy.sh/my-topic")
    assert n.send("s", "b") is False
    assert "notify" in capsys.readouterr().err


def test_empty_ntfy_url_never_constructs_a_notifier():
    s = Settings(_env_file=None)
    assert not isinstance(build_notifier(s), NtfyNotifier)
