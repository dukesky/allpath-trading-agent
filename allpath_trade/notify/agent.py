from __future__ import annotations

import json
import sys
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

from allpath_trade.notify.base import MultiNotifier, Notifier, header_safe

_AGENT_TIMEOUT_SECONDS = 10
_TOKEN_MASK = "***"
PAYLOAD_VERSION = 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AgentWebhookNotifier(Notifier):
    """Machine-readable channel for an external executor agent
    (AGENT_WEBHOOK_URL, .env-only): every notification becomes one JSON POST
    built from the subject's structured `.event` (notify/events.py's
    `Subject`) -- `{"v", "ts", "type", "account", "subject", ...}`.

    The human `body` is deliberately never sent: it can carry a one-time
    approve link, and an agent should act through the CLI
    (`allpath-trade reviews ... --json` / `approve` / `reject`), which
    re-prices and re-runs every guard, not by following a link. A subject
    without `.event` (a test send, a future builder) degrades to
    `type: "notification"`.

    Works with any JSON webhook, and with an ntfy topic URL too (the JSON
    arrives as the message text; `Title` gives the phone banner). The
    optional `token` is sent as `Authorization: Bearer` -- ntfy access
    tokens and most webhook receivers accept that shape. Same never-raises
    contract as every other Notifier."""

    def __init__(self, url: str, token: str = "",
                 clock: Callable[[], str] = _now_iso) -> None:
        self.url = url
        self.token = token
        self._clock = clock

    def payload(self, subject: str) -> dict:
        event = getattr(subject, "event", None) or {"type": "notification"}
        return {"v": PAYLOAD_VERSION, "ts": self._clock(), **event,
                "subject": str(subject)}

    def send(self, subject: str, body: str) -> bool:
        try:
            payload = self.payload(subject)
            headers = {"Content-Type": "application/json",
                       "Title": header_safe(str(subject)),
                       "X-AllPath-Event": str(payload["type"])}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            req = urllib.request.Request(
                self.url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=_AGENT_TIMEOUT_SECONDS) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
        except Exception as exc:  # noqa: BLE001 — notification must not crash callers
            msg = str(exc)
            if self.token:
                msg = msg.replace(self.token, _TOKEN_MASK)
            print(f"[notify] agent webhook send failed: {msg}", file=sys.stderr)
            return False
        if 200 <= status < 300:
            return True
        print(f"[notify] agent webhook send failed: HTTP {status}", file=sys.stderr)
        return False


def send_agent_only(notifier: Notifier | None, subject: str, body: str) -> None:
    """Deliver to the agent channel(s) only, skipping every human channel.

    Used where a per-strategy `notify_email: false` mutes the human inbox:
    that is a preference about Tian's phone and email, not about the
    executor agent's feed, which has to see every queued review to be
    useful at all."""
    if isinstance(notifier, AgentWebhookNotifier):
        notifier.send(subject, body)
    elif isinstance(notifier, MultiNotifier):
        for child in notifier.children:
            send_agent_only(child, subject, body)
