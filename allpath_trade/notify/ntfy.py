from __future__ import annotations

import sys
import urllib.request

from allpath_trade.notify.base import Notifier

_NTFY_TIMEOUT_SECONDS = 10


class NtfyNotifier(Notifier):
    """Push notification channel via ntfy (https://ntfy.sh or a self-hosted
    server): the user installs the ntfy app, subscribes to a topic, and this
    just POSTs the body to that topic URL -- no account, no API key, one
    HTTP call, instant phone banner.

    Uses stdlib `urllib.request` rather than `httpx`: httpx is a dev-only
    dependency (the FastAPI TestClient pulls it in), not a runtime one --
    see pyproject.toml's `[dependency-groups] dev`. Promoting it to a
    runtime dependency for a single POST call isn't worth it when
    `models_catalog.py` (Task 5) already established the stdlib precedent
    for exactly this shape of one-call HTTP.
    """

    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, subject: str, body: str) -> bool:
        try:
            # Request(...) itself can raise (e.g. ValueError("unknown url
            # type") for a scheme-less self.url) -- Settings validates
            # ntfy_url before it ever reaches here, but this notifier's
            # never-raises contract has to hold regardless of where the URL
            # came from, so construction stays inside the try too.
            req = urllib.request.Request(
                self.url,
                data=body.encode("utf-8"),
                headers={"Title": subject},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_NTFY_TIMEOUT_SECONDS) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
        except Exception as exc:  # noqa: BLE001 — notification must not crash callers
            print(f"[notify] ntfy send failed: {exc}", file=sys.stderr)
            return False
        if 200 <= status < 300:
            return True
        print(f"[notify] ntfy send failed: HTTP {status}", file=sys.stderr)
        return False
