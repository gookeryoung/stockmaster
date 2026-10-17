"""Alert notification channels.

The default is database-only (triggers are always persisted). Outbound delivery
is pluggable and explicitly opt-in: a generic JSON webhook suitable for
Server酱/钉钉/企微 relays, an optional direct Bark-style push, and SMTP email.

With data isolation on, outbound delivery is routed **per workspace**: a trigger
belongs to the workspace that owns its rule, so its notification must go to that
workspace's own channels instead of one global webhook. With isolation off the
old global behaviour is preserved byte for byte — that is what lets the switch be
flipped on and rolled back without changing delivery semantics.

The optional ``workspaces.message_template`` is applied per workspace (body only —
the title stays the short trigger title used as email subject / push title; the
template can embed ``{title}`` if it wants to). A broken template at runtime must
never suppress an alert, so formatting failures fall back to the original text.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from stockmaster.config import settings
from stockmaster.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "BarkChannel",
    "Channel",
    "DatabaseChannel",
    "EmailChannel",
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


class EmailChannel:
    """SMTP 邮件通道：收件人串（逗号/分号分隔）存在通道的 ``url`` 列里。

    SMTP 是部署级凭据（``SM_SMTP_*``），不像 webhook 那样属于某个工作区；工作区只
    决定「发给谁」。没有配置 ``SM_SMTP_HOST`` 或没有收件人时直接返回 False —— 未配置
    邮件外发不应该被当成错误（与未配置全局 webhook 时一致）。
    """

    name = "email"

    def __init__(self, recipients: str, timeout: float | None = None) -> None:
        self.recipients = _split_recipients(recipients)
        self.timeout = settings.smtp_timeout if timeout is None else timeout

    def send(self, notification: Notification) -> bool:
        if not settings.smtp_host or not self.recipients:
            return False
        try:
            import smtplib
            from email.message import EmailMessage

            message = EmailMessage()
            message["Subject"] = notification.title
            message["From"] = settings.smtp_from or settings.smtp_username or "stockmaster@localhost"
            message["To"] = ", ".join(self.recipients)
            message.set_content(notification.body)

            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=self.timeout) as smtp:
                if settings.smtp_tls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
            return True
        except Exception as exc:
            # 邮件失败与 webhook 失败同一处理：只记日志，不中断扫描/回滚触发记录。
            log.warning("email delivery failed: %s", exc)
            return False


def _split_recipients(value: str) -> list[str]:
    """把 ``a@x.com, b@y.com`` / ``a@x.com;b@y.com`` 拆成地址列表。"""
    return [item.strip() for item in (value or "").replace(";", ",").split(",") if item.strip()]


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
    if kind == "email":
        return EmailChannel(url)
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


def _workspace_templates(session, workspace_ids: list[int]) -> dict[int, str]:
    """一次查询批量取非空消息模板；``workspace_id <= 0`` 永远不套模板。"""
    from sqlalchemy import select

    from stockmaster.db.models import Workspace

    ids = [int(workspace_id) for workspace_id in dict.fromkeys(workspace_ids) if workspace_id > 0]
    if not ids:
        return {}
    rows = session.execute(select(Workspace.id, Workspace.message_template).where(Workspace.id.in_(ids))).all()
    return {int(workspace_id): (template or "") for workspace_id, template in rows if template}


def _apply_template(template: str, notification: Notification) -> Notification:
    """按工作区模板重写正文；标题保持原样（邮件主题 / Bark 推送需要短标题）。

    模板写错（未知占位符、格式说明符不匹配等）时只 log warning 并回退原通知：
    一条通知的样式问题不能变成「预警没发出去」。
    """
    try:
        body = template.format(
            title=notification.title,
            body=notification.body,
            code=notification.code,
            rule_id=notification.rule_id,
            level=notification.level,
        )
    except Exception as exc:
        log.warning("workspace message template failed, falling back to original text: %s", exc)
        return notification
    return replace(notification, body=body)


def notify(notifications: list[Notification]) -> dict[str, int]:
    """Deliver notifications through the channels their workspace routes to.

    Returns per-channel success counts for reporting. With isolation off the keys
    are plain channel names (``database``/``webhook``) and every notification goes
    through the global list — the original behaviour, with no DB session opened.

    With isolation on each ``workspace_id`` group is resolved separately and the
    keys carry a ``ws{id}:`` prefix (e.g. ``ws3:webhook``) so a report shows which
    workspace delivered what. The same session is reused to batch-load the
    workspaces' message templates once, then each group's notifications are
    rewritten before delivery (``workspace_id <= 0`` never uses a template).
    """
    if not settings.data_isolation:
        return _deliver(get_channels(), notifications)

    from stockmaster.db.session import session_scope

    results: dict[str, int] = {}
    with session_scope() as session:
        groups = _group_by_workspace(notifications)
        templates = _workspace_templates(session, list(groups))
        for workspace_id, batch in groups.items():
            template = templates.get(workspace_id)
            if template:
                batch = [_apply_template(template, notification) for notification in batch]
            channels = channels_for_workspace(session, workspace_id)
            for key, delivered in _deliver(channels, batch, prefix=f"ws{workspace_id}:").items():
                results[key] = results.get(key, 0) + delivered
    return results
