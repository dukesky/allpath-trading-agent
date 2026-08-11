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


def send_report(notifier: Notifier, subject: str, summary_body: str,
                full_body: str) -> bool:
    """Route the daily reflection notification (Phase 6) with a body chosen
    per notifier type: a phone-banner push channel wants the short
    `summary_body`; a channel that renders arbitrarily long text without
    complaint wants the full report.

    Same per-type dispatch the removed `send_test_notification` (notify/
    base.py before commit 32d41ad -- the settings page's old save-and-test
    button) used, so the web route only had to know outcome codes, never
    SMTP/ntfy field semantics; here the caller (Reflector.run_daily) only
    needs to know "notify", not which channel wants which body.

    - `MultiNotifier` recurses into every child with the SAME eager-list
      (not `any(generator)`) convention `send_each` already uses -- a dead
      email channel must not skip a live ntfy child, or vice versa -- then
      reports the coarse any-of-them-worked truth, matching `send`.
    - `NtfyNotifier` gets `summary_body` -- it's a push banner, not an
      inbox.
    - Everything else (`EmailNotifier`, `ConsoleNotifier`, and any future
      or unrecognized notifier type) gets `full_body`. Falling through to
      the full body by default, rather than requiring an explicit
      isinstance branch per known type, is deliberate: the richer of the
      two bodies is the safer default when the channel isn't known to be
      push-shaped.

    Imports NtfyNotifier lazily (not at module level): both ntfy.py and
    email.py import `Notifier` from this module, so importing them back
    here at module load time would be circular.
    """
    from allpath_trade.notify.ntfy import NtfyNotifier

    if isinstance(notifier, MultiNotifier):
        results = [send_report(child, subject, summary_body, full_body)
                  for child in notifier.children]
        return any(results)
    if isinstance(notifier, NtfyNotifier):
        return notifier.send(subject, summary_body)
    return notifier.send(subject, full_body)
