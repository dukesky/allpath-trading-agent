from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, body: str) -> None: ...


class ConsoleNotifier(Notifier):
    def send(self, subject: str, body: str) -> None:
        print(f"[notify] {subject}\n{body}")
