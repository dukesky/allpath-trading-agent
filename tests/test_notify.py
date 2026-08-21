from typing import ClassVar

from allpath_trade.config import Settings
from allpath_trade.notify import events
from allpath_trade.notify.base import ConsoleNotifier, MultiNotifier, send_report
from allpath_trade.notify.email import EmailNotifier, build_notifier
from allpath_trade.notify.ntfy import NtfyNotifier


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


def test_build_notifier_selects_ntfy_alone(tmp_path):
    s = Settings(_env_file=tmp_path / "none.env", ntfy_url="https://ntfy.sh/my-topic")
    n = build_notifier(s)
    assert isinstance(n, NtfyNotifier)
    assert n.url == "https://ntfy.sh/my-topic"


def test_build_notifier_composes_both_into_a_multinotifier(tmp_path):
    s = Settings(_env_file=tmp_path / "none.env", smtp_host="smtp.x.com",
                notify_to="t@x.com", ntfy_url="https://ntfy.sh/my-topic")
    n = build_notifier(s)
    assert isinstance(n, MultiNotifier)
    kinds = {type(c) for c in n.children}
    assert kinds == {EmailNotifier, NtfyNotifier}


class _RecordingNotifier:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> bool:
        self.calls.append((subject, body))
        return self.ok


def test_multi_notifier_calls_every_child_even_when_the_first_fails():
    failing = _RecordingNotifier(ok=False)
    working = _RecordingNotifier(ok=True)
    multi = MultiNotifier([failing, working])
    assert multi.send("s", "b") is True
    assert failing.calls == [("s", "b")]
    assert working.calls == [("s", "b")]  # not skipped just because it's second


def test_multi_notifier_calls_every_child_even_when_the_first_succeeds():
    working = _RecordingNotifier(ok=True)
    other = _RecordingNotifier(ok=True)
    multi = MultiNotifier([working, other])
    multi.send("s", "b")
    # any(generator) would short-circuit after the first True and never call
    # `other` -- both channels are always supposed to actually fire.
    assert other.calls == [("s", "b")]


def test_multi_notifier_send_is_true_if_any_child_delivered():
    assert MultiNotifier([_RecordingNotifier(False), _RecordingNotifier(True)]).send(
        "s", "b") is True
    assert MultiNotifier([_RecordingNotifier(False), _RecordingNotifier(False)]).send(
        "s", "b") is False


def test_multi_notifier_send_each_reports_per_channel_results():
    multi = MultiNotifier([_RecordingNotifier(True), _RecordingNotifier(False)])
    assert multi.send_each("s", "b") == [True, False]


# -- daily_report / send_report (Task 5: reflection notifications) --


def test_daily_report_subject_and_body():
    subject, full_body = events.daily_report(
        account="paper", date="2026-08-10", summary="Quiet day overall.",
        body="Day summary: ...")
    assert subject == "[Paper] [AllPath] Daily reflection 2026-08-10"
    assert "Quiet day overall." in full_body
    assert "Day summary: ..." in full_body
    assert full_body.endswith(events.FOOTER)


class _SpyNtfy(NtfyNotifier):
    """Real NtfyNotifier subclass (so isinstance() dispatch in send_report
    sees the real type) that records calls instead of hitting the network."""

    def __init__(self):
        super().__init__("https://ntfy.sh/x")
        self.calls: list[tuple[str, str]] = []

    def send(self, subject, body):
        self.calls.append((subject, body))
        return True


class _SpyEmail(EmailNotifier):
    """Real EmailNotifier subclass, same reasoning as _SpyNtfy."""

    def __init__(self):
        super().__init__("h", 1, "u", "p", "f@x.com", "t@x.com")
        self.calls: list[tuple[str, str]] = []

    def send(self, subject, body):
        self.calls.append((subject, body))
        return True


def test_send_report_gives_ntfy_the_summary_and_email_the_full_body():
    ntfy_child = _SpyNtfy()
    email_child = _SpyEmail()
    multi = MultiNotifier([ntfy_child, email_child])

    result = send_report(multi, "subj", "short summary", "full report text")

    assert result is True
    assert ntfy_child.calls == [("subj", "short summary")]
    assert email_child.calls == [("subj", "full report text")]


def test_send_report_multinotifier_any_success_and_both_children_attempted():
    failing = _RecordingNotifier(False)
    working = _RecordingNotifier(True)
    multi = MultiNotifier([failing, working])

    assert send_report(multi, "s", "summary", "full") is True
    assert failing.calls == [("s", "full")]  # unknown type -> full_body
    assert working.calls == [("s", "full")]


def test_send_report_unknown_notifier_type_gets_full_body():
    notifier = _RecordingNotifier(True)
    assert send_report(notifier, "s", "summary", "full") is True
    assert notifier.calls == [("s", "full")]


def test_send_report_console_and_email_get_full_body():
    console = ConsoleNotifier()
    result = send_report(console, "s", "short", "long report")
    assert result is True
