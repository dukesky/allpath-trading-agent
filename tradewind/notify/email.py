from __future__ import annotations

import functools
import smtplib
import sys
from collections.abc import Callable
from email.message import EmailMessage

from tradewind.config import Settings
from tradewind.notify.base import ConsoleNotifier, Notifier

_SMTP_TIMEOUT_SECONDS = 10


class EmailNotifier(Notifier):
    """Email is a notification-only channel: bodies never contain action
    links, and a send failure must never break the caller."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 sender: str, to: str,
                 smtp_factory: Callable | None = None) -> None:
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.sender, self.to = sender, to
        # No injected factory: build the real smtplib.SMTP with a connect
        # timeout so a hung SMTP server can't wedge the sentinel loop.
        # Injected stubs (tests) keep their own (host, port) signature.
        self._smtp = smtp_factory or functools.partial(
            smtplib.SMTP, timeout=_SMTP_TIMEOUT_SECONDS)

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender or self.user
        msg["To"] = self.to
        msg.set_content(body)
        try:
            with self._smtp(self.host, self.port) as smtp:
                smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 — notification must not crash callers
            print(f"[notify] email send failed: {exc}", file=sys.stderr)


def build_notifier(settings: Settings) -> Notifier:
    if settings.smtp_host and settings.notify_to:
        return EmailNotifier(settings.smtp_host, settings.smtp_port,
                             settings.smtp_user, settings.smtp_password,
                             settings.smtp_from, settings.notify_to)
    return ConsoleNotifier()
