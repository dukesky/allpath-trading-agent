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
