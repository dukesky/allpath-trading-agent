from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, body: str) -> bool:
        """Return whether the notification was actually delivered.

        Implementations must not raise -- a broken notification channel must
        never break the caller (the sentinel loop, a settings-page test
        button). The return value is the only way a caller can tell delivery
        apart from a silent failure, so it is load-bearing, not decorative."""
        ...


class ConsoleNotifier(Notifier):
    def send(self, subject: str, body: str) -> bool:
        print(f"[notify] {subject}\n{body}")
        return True


class MultiNotifier(Notifier):
    """Fans out one notification to every configured channel. A dead email
    channel must not eat the push, and vice versa -- so every child is
    always attempted, regardless of whether an earlier one already
    succeeded or failed. `send` reports the coarse any-of-them-worked
    truth; `send_each` exposes the raw per-channel results for a caller
    (the settings page's save-and-test button) that needs to tell the user
    which channel actually worked."""

    def __init__(self, children: list[Notifier]) -> None:
        self.children = children

    def send_each(self, subject: str, body: str) -> list[bool]:
        # Eager list comprehension, not `any(gen)`: a generator passed to
        # any() stops at the first True, which would silently skip later
        # children on a call where an earlier one already succeeded.
        return [child.send(subject, body) for child in self.children]

    def send(self, subject: str, body: str) -> bool:
        return any(self.send_each(subject, body))


def send_test_notification(notifier: Notifier, subject: str, body: str) -> str:
    """Send a real test notification and return a note code the settings
    page can render.

    Dispatches on the notifier's own type rather than asking the caller to
    re-derive "which channels are configured" from Settings fields -- the
    single source of truth for that question is already `build_notifier`
    (email.py), which is exactly what decided whether this notifier is a
    ConsoleNotifier, a single real channel, or a MultiNotifier of several.
    Keeping the dispatch here means the web route only needs to know the
    four outcome codes, not SMTP/ntfy field semantics.

    - "test_none": nothing beyond the no-op console fallback is configured.
      ConsoleNotifier.send() always returns True, but nothing was actually
      attempted -- reporting "sent" here would be a lie the user acts on.
    - "test_ok": every configured channel delivered.
    - "test_partial": some configured channels delivered, some didn't.
    - "test_failed": none delivered.
    """
    if isinstance(notifier, ConsoleNotifier):
        return "test_none"
    if isinstance(notifier, MultiNotifier):
        results = notifier.send_each(subject, body)
    else:
        results = [notifier.send(subject, body)]
    delivered = sum(results)
    if delivered == len(results):
        return "test_ok"
    if delivered == 0:
        return "test_failed"
    return "test_partial"
