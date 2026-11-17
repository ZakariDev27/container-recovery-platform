"""Alerts: POST serious events to a webhook (Slack-compatible JSON)."""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("recovery")

# Events worth waking someone up for. Routine ones (replaced, backup_created) are left out.
DEFAULT_EVENTS = (
    "circuit_opened",
    "replace_failed",
    "storage_restored",
    "restore_failed",
    "restore_unavailable",
    "backup_failed",
    "backup_verify_failed",
)


class WebhookNotifier:
    def __init__(self, url: str, events: tuple[str, ...] = DEFAULT_EVENTS, timeout: float = 5.0):
        self.url = url
        self.events = set(events)
        self.timeout = timeout

    def __call__(self, event: dict) -> None:
        if event["event"] not in self.events:
            return
        extras = ", ".join(
            f"{k}={v}" for k, v in event.items() if k not in ("time", "event", "service")
        )
        text = f"[crp] {event['service']}: {event['event']}" + (f" ({extras})" if extras else "")
        body = json.dumps({"text": text, **event}).encode()
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except (OSError, ValueError) as exc:
            # An unreachable alert endpoint must never stop recovery work.
            log.warning("alert webhook failed: %s", exc)
