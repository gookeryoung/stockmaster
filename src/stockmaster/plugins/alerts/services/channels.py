"""Alert notification channels.

The default is database-only (triggers are always persisted). Outbound delivery
is pluggable and explicitly opt-in: a generic JSON webhook suitable for
Server酱/钉钉/企微 relays, and an optional direct Bark-style push. Network
failures are logged and never propagated, because a failed notification must not
roll back a trigger or break the scan loop.

With data isolation on, outbound delivery is routed **per workspace**: a trigger
belongs to the workspace that owns its rule, so its notification must go to that
workspace's own channels instead of one global webhook. With isolation off the
old global behaviour is preserved byte for byte — that is what lets the switch be
flipped on and rolled back without changing delivery semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from stockmaster.config import settings
from stockmaster.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "BarkChannel",
    "Channel",
    "DatabaseChannel",
    "Notification",
    "WebhookChannel",
    "build_channel",
    "channels_for_workspace",
    "get_channels",
    "notify",
    "send_test",
]


@dataclass
class Notification:
    """A message to deliver."""

    title: str
    body: str
    code: str = ""
    rule_id: int | None = None
    level: str = "info"  # info | warning | critical
    # 冗余自触发记录：投递路由要按工作区解析通道，不能依赖回查 alert_rules。
    # 0 = 未归属（历史数据 / 隔离关闭），始终走全局通道。
    workspace_id: int = 0


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


def build_channel(row) -> Channel | None:
    """Build a delivery channel from a persisted ``workspace_channels`` row.

    Returns ``None`` for an unknown ``kind`` instead of raising: a row written by
    a newer version (or edited by hand) must not break delivery for every other
    workspace.
    """
    kind = (getattr(row, "kind", "") or "").strip().lower()
    url = getattr(row, "url", "") or ""
    if kind == "webhook":
        return WebhookChannel(url)
    if kind == "bark":
        return BarkChannel(url)
    return None


def _workspace_channels(session, workspace_id: int) -> list[Channel]:
    """Load a workspace's enabled channel rows (isolation is assumed to be on)."""
    from sqlalchemy import select

    from stockmaster.db.models import WorkspaceChannel

    rows = (
        session.execute(
            select(WorkspaceChannel)
            .where(
                WorkspaceChannel.workspace_id == workspace_id,
                WorkspaceChannel.enabled.is_(True),
            )
            .order_by(WorkspaceChannel.id)
        )
        .scalars()
        .all()
    )
    return [channel for row in rows if (channel := build_channel(row)) is not None]


def channels_for_workspace(session, workspace_id: int) -> list[Channel]:
    """Resolve the delivery channels for one workspace.

    * Isolation off, or an unassigned trigger (``workspace_id <= 0``): the global
      channel list, i.e. exactly the pre-isolation behaviour.
    * Isolation on: the database sink plus the workspace's own enabled channels.
      Strict by default — no channel means no outbound delivery. Setting
      ``settings.alert_global_fallback`` adds the global webhook only when the
      workspace has no enabled channel at all, for the transition period where
      isolation is on but channels are not configured yet.
    """
    if not settings.data_isolation or workspace_id <= 0:
        return get_channels()

    channels: list[Channel] = [DatabaseChannel()]
    channels.extend(_workspace_channels(session, workspace_id))
    if len(channels) == 1 and settings.alert_global_fallback and settings.alert_webhook_url:
        # 严格隔离是默认：宁可不外发，也不能把 A 工作区的触发内容送进全站群机器人。
        channels.append(WebhookChannel(settings.alert_webhook_url))
    return channels


def send_test(row) -> bool:
    """Send one test notification through a ``workspace_channels`` row.

    Backs the workspace channel "test" endpoint. A misconfigured URL or a network
    failure yields ``False`` (reported as ``delivered=false``) instead of a 500;
    nothing is ever raised.
    """
    try:
        channel = build_channel(row)
        if channel is None:
            log.warning("test notification skipped: unknown channel kind %r", getattr(row, "kind", ""))
            return False
        notification = Notification(
            title=f"[测试] {getattr(row, 'name', '') or '通知通道'}",
            body="这是一条 StockMaster 测试通知，收到即表示通道配置可用。",
            workspace_id=int(getattr(row, "workspace_id", 0) or 0),
        )
        return bool(channel.send(notification))
    except Exception as exc:
        log.warning("test notification failed: %s", exc)
        return False


def _deliver(channels: list[Channel], notifications: list[Notification], prefix: str = "") -> dict[str, int]:
    """Deliver a batch through the given channels and count successes."""
    results: dict[str, int] = {}
    for channel in channels:
        delivered = 0
        for notification in notifications:
            try:
                if channel.send(notification):
                    delivered += 1
            except Exception as exc:
                log.warning("channel %s raised: %s", channel.name, exc)
        key = f"{prefix}{channel.name}"
        results[key] = results.get(key, 0) + delivered
    return results


def _group_by_workspace(notifications: list[Notification]) -> dict[int, list[Notification]]:
    """Group notifications by workspace, keeping first-seen order."""
    groups: dict[int, list[Notification]] = {}
    for notification in notifications:
        groups.setdefault(notification.workspace_id, []).append(notification)
    return groups


def notify(notifications: list[Notification]) -> dict[str, int]:
    """Deliver notifications through the channels their workspace routes to.

    Returns per-channel success counts for reporting. With isolation off the keys
    are plain channel names (``database``/``webhook``) and every notification goes
    through the global list — the original behaviour, with no DB session opened.
    With isolation on each ``workspace_id`` group is resolved separately and the
    keys carry a ``ws{id}:`` prefix (e.g. ``ws3:webhook``) so a report shows which
    workspace delivered what.
    """
    if not settings.data_isolation:
        return _deliver(get_channels(), notifications)

    from stockmaster.db.session import session_scope

    results: dict[str, int] = {}
    with session_scope() as session:
        for workspace_id, batch in _group_by_workspace(notifications).items():
            channels = channels_for_workspace(session, workspace_id)
            for key, delivered in _deliver(channels, batch, prefix=f"ws{workspace_id}:").items():
                results[key] = results.get(key, 0) + delivered
    return results
