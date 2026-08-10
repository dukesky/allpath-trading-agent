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
