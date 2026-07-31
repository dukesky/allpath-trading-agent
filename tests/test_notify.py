from tradewind.config import Settings
from tradewind.notify.base import ConsoleNotifier
from tradewind.notify.email import EmailNotifier, build_notifier


class StubSMTP:
    instances = []

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
    ConsoleNotifier().send("subj", "body")
    out = capsys.readouterr().out
    assert "subj" in out and "body" in out


def test_email_notifier_sends():
    n = EmailNotifier("smtp.x.com", 587, "u", "p", "from@x.com", "to@x.com",
                      smtp_factory=StubSMTP)
    n.send("Trigger: AAPL", "details")
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
    n.send("s", "b")  # must not raise


def test_build_notifier_selects(tmp_path):
    s = Settings(_env_file=tmp_path / "none.env")
    assert isinstance(build_notifier(s), ConsoleNotifier)
    s2 = s.model_copy(update={"smtp_host": "smtp.x.com", "notify_to": "t@x.com"})
    assert isinstance(build_notifier(s2), EmailNotifier)
