"""Alert notification channels.

The default is database-only (triggers are always persisted). Outbound delivery
is pluggable and explicitly opt-in: a generic JSON webhook suitable for
Server酱/钉钉/企微 relays, and an optional direct Bark-style push. Network
failures are logged and never propagated, because a failed notification must not
roll back a trigger or break the scan loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from stockmaster.config import settings
from stockmaster.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["Channel", "DatabaseChannel", "Notification", "WebhookChannel", "get_channels"]


@dataclass
class Notification:
    """A message to deliver."""

    title: str
    body: str
    code: str = ""
    rule_id: int | None = None
    level: str = "info"  # info | warning | critical


class Channel(Protocol):
    """A delivery target for notifications."""

    name: str

    def send(self, notification: Notification) -> bool:
        """Deliver a notification. Returns success; never raises for network errors."""
        ...


class DatabaseChannel:
    """No-op sink: triggers are already persisted by the engine.

    Exists so the channel list is never empty and the pipeline has a defined
    default that requires no configuration.
    """

    name = "database"

    def send(self, notification: Notification) -> bool:
        return True


class WebhookChannel:
    """POST a JSON payload to a configured URL.

    Payload shape is intentionally simple so it can be relayed by a Server酱,
    钉钉 or 企业微信 bridge without app-specific logic.
    """

    name = "webhook"

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, notification: Notification) -> bool:
        if not self.url:
            return False
        payload = {
            "title": notification.title,
            "body": notification.body,
            "code": notification.code,
            "rule_id": notification.rule_id,
            "level": notification.level,
        }
        try:
            import httpx

            response = httpx.post(self.url, json=payload, timeout=self.timeout)
            ok = 200 <= response.status_code < 300
            if not ok:
                log.warning("webhook returned %s for %s", response.status_code, notification.code)
            return ok
        except Exception as exc:
            log.warning("webhook delivery failed: %s", exc)
            return False


class BarkChannel:
    """Bark-style iOS push: GET /<key>/<title>/<body>."""

    name = "bark"

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def send(self, notification: Notification) -> bool:
        if not self.base_url:
            return False
        from urllib.parse import quote

        url = f"{self.base_url}/{quote(notification.title)}/{quote(notification.body)}"
        try:
            import httpx

            response = httpx.get(url, timeout=self.timeout)
            return 200 <= response.status_code < 300
        except Exception as exc:
            log.warning("bark delivery failed: %s", exc)
            return False


def get_channels() -> list[Channel]:
    """Build the configured channel list.

    Always includes the database sink. Outbound channels appear only when their
    configuration is present, so an unconfigured install sends nothing externally.
    """
    channels: list[Channel] = [DatabaseChannel()]
    if settings.alert_webhook_url:
        channels.append(WebhookChannel(settings.alert_webhook_url))
    return channels


def notify(notifications: list[Notification]) -> dict[str, int]:
    """Deliver notifications through every configured channel.

    Returns per-channel success counts for reporting.
    """
    channels = get_channels()
    results: dict[str, int] = {}
    for channel in channels:
        delivered = 0
        for notification in notifications:
            try:
                if channel.send(notification):
                    delivered += 1
            except Exception as exc:
                log.warning("channel %s raised: %s", channel.name, exc)
        results[channel.name] = delivered
    return results
