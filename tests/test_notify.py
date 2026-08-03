from typing import ClassVar

from allpath_trade.config import Settings
from allpath_trade.notify.base import ConsoleNotifier
from allpath_trade.notify.email import EmailNotifier, build_notifier


class StubSMTP:
    instances: ClassVar[list["StubSMTP"]] = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sent = []
        StubSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_console_notifier_prints(capsys):
    assert ConsoleNotifier().send("subj", "body") is True
    out = capsys.readouterr().out
    assert "subj" in out and "body" in out


def test_email_notifier_sends():
    n = EmailNotifier("smtp.x.com", 587, "u", "p", "from@x.com", "to@x.com",
                      smtp_factory=StubSMTP)
    assert n.send("Trigger: AAPL", "details") is True
    smtp = StubSMTP.instances[-1]
    assert smtp.tls and smtp.creds == ("u", "p")
    [msg] = smtp.sent
    assert msg["Subject"] == "Trigger: AAPL"
    assert msg["To"] == "to@x.com"


def test_email_failure_does_not_raise(capsys):
    def broken(host, port):
        raise OSError("connection refused")

    n = EmailNotifier("smtp.x.com", 587, "u", "p", "f@x.com", "t@x.com",
                      smtp_factory=broken)
    # must not raise -- and the caller must be able to tell it failed.
    assert n.send("s", "b") is False


def test_default_smtp_factory_passes_timeout(monkeypatch):
    calls = []

    class RecordingSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append((host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            pass

    monkeypatch.setattr("smtplib.SMTP", RecordingSMTP)
    n = EmailNotifier("smtp.x.com", 587, "", "", "f@x.com", "t@x.com")
    n.send("s", "b")
    assert calls == [("smtp.x.com", 587, 10)]


def test_build_notifier_selects(tmp_path):
    s = Settings(_env_file=tmp_path / "none.env")
    assert isinstance(build_notifier(s), ConsoleNotifier)
    s2 = s.model_copy(update={"smtp_host": "smtp.x.com", "notify_to": "t@x.com"})
    assert isinstance(build_notifier(s2), EmailNotifier)
