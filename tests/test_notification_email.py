"""邮件通知通道 + 按工作区消息模板。

离线约束
-------
邮件发送全部打桩：``smtplib.SMTP`` 换成记录型假实现，绝不发真实网络请求。真实的
SMTP 凭据是部署级配置（``SM_SMTP_*``），测试只验证「配置齐全时按预期调用
STARTTLS/login/send_message」「缺配置或异常时返回 False 而不抛」。

模板语义
-------
``workspaces.message_template`` 作用于外发通知的**正文**；标题保持原样（邮件主题 /
Bark 推送标题需要短标题，模板里可以用 ``{title}`` 自行拼接完整文本）。模板写错在 API
保存时 422；运行期格式化失败则 log 后回退原文，绝不能让模板问题吞掉预警。
"""

from __future__ import annotations

import smtplib
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"
SECRET_EMAIL = "alice@example.com"
SECOND_EMAIL = "bob@example.com"


# --------------------------------------------------------------------------- #
# SMTP 打桩
# --------------------------------------------------------------------------- #


class _FakeSMTP:
    """最小 SMTP 替身：记录构造参数、STARTTLS、登录与消息。"""

    created: list[_FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_calls = 0
        self.login_args: tuple[str, str] | None = None
        self.messages: list = []
        self.closed = False
        _FakeSMTP.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def starttls(self):
        self.starttls_calls += 1

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.messages.append(message)


@pytest.fixture
def smtp_config(monkeypatch):
    """配置齐全的 SMTP + 假 SMTP；返回 settings 以便逐项断言。"""
    from stockmaster.config import settings

    _FakeSMTP.created = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 2525)
    monkeypatch.setattr(settings, "smtp_username", "bot")
    monkeypatch.setattr(settings, "smtp_password", "smtp-secret")
    monkeypatch.setattr(settings, "smtp_from", "bot@example.com")
    monkeypatch.setattr(settings, "smtp_tls", True)
    monkeypatch.setattr(settings, "smtp_timeout", 3.0)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return settings


# --------------------------------------------------------------------------- #
# EmailChannel 单元测试
# --------------------------------------------------------------------------- #


def test_email_channel_without_smtp_host_returns_false(monkeypatch):
    from stockmaster.config import settings
    from stockmaster.plugins.alerts.services.channels import EmailChannel, Notification

    monkeypatch.setattr(settings, "smtp_host", "")

    def no_network(*args, **kwargs):
        raise AssertionError("没有配置 SMTP host 时不应打开连接")

    monkeypatch.setattr(smtplib, "SMTP", no_network)
    assert EmailChannel(SECRET_EMAIL).send(Notification(title="t", body="b")) is False


def test_email_channel_without_recipients_returns_false(smtp_config):
    from stockmaster.plugins.alerts.services.channels import EmailChannel, Notification

    _FakeSMTP.created = []
    assert EmailChannel("").send(Notification(title="t", body="b")) is False
    assert EmailChannel("  ; , ").recipients == []
    assert _FakeSMTP.created == []


def test_email_channel_uses_tls_login_and_builds_message(smtp_config):
    from stockmaster.plugins.alerts.services.channels import EmailChannel, Notification

    channel = EmailChannel(f"{SECRET_EMAIL}; {SECOND_EMAIL}")
    assert channel.recipients == [SECRET_EMAIL, SECOND_EMAIL]

    sent = channel.send(Notification(title="预警：600519.SH", body="收盘价 100.00 上穿 90.00", code="600519.SH"))
    assert sent is True

    (smtp,) = _FakeSMTP.created
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 2525, 3.0)
    assert smtp.starttls_calls == 1
    assert smtp.login_args == ("bot", "smtp-secret")
    assert smtp.closed is True
    (message,) = smtp.messages
    assert message["Subject"] == "预警：600519.SH"
    assert message["From"] == "bot@example.com"
    assert message["To"] == f"{SECRET_EMAIL}, {SECOND_EMAIL}"
    assert message.get_content().strip() == "收盘价 100.00 上穿 90.00"


def test_email_channel_skips_tls_and_login_when_not_configured(smtp_config, monkeypatch):
    from stockmaster.plugins.alerts.services.channels import EmailChannel, Notification

    monkeypatch.setattr(smtp_config, "smtp_tls", False)
    monkeypatch.setattr(smtp_config, "smtp_username", "")
    _FakeSMTP.created = []

    assert EmailChannel(SECRET_EMAIL).send(Notification(title="t", body="b")) is True
    (smtp,) = _FakeSMTP.created
    assert smtp.starttls_calls == 0
    assert smtp.login_args is None


def test_email_channel_swallows_smtp_exceptions(smtp_config, monkeypatch):
    from stockmaster.plugins.alerts.services.channels import EmailChannel, Notification

    def boom(*args, **kwargs):
        raise smtplib.SMTPException("smtp down")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    assert EmailChannel(SECRET_EMAIL).send(Notification(title="t", body="b")) is False


# --------------------------------------------------------------------------- #
# build_channel / send_test 的 email 分支
# --------------------------------------------------------------------------- #


def test_build_channel_and_send_test_for_email(smtp_config):
    from stockmaster.plugins.alerts.services.channels import (
        EmailChannel,
        build_channel,
        send_test,
    )

    row = SimpleNamespace(kind="email", url=f"{SECRET_EMAIL}, {SECOND_EMAIL}", name="邮件", workspace_id=5)
    channel = build_channel(row)
    assert isinstance(channel, EmailChannel)

    _FakeSMTP.created = []
    assert send_test(row) is True
    assert len(_FakeSMTP.created) == 1
    assert "测试" in _FakeSMTP.created[0].messages[0]["Subject"]


def test_send_test_email_without_smtp_config_returns_false(monkeypatch):
    from stockmaster.config import settings
    from stockmaster.plugins.alerts.services.channels import send_test

    monkeypatch.setattr(settings, "smtp_host", "")
    row = SimpleNamespace(kind="email", url=SECRET_EMAIL, name="邮件", workspace_id=5)
    assert send_test(row) is False


# --------------------------------------------------------------------------- #
# 通道 API：email 校验、掩码与角色矩阵
# --------------------------------------------------------------------------- #


@pytest.fixture
def email_client(tmp_path, monkeypatch):
    """四个账号：alice(admin/owner) · bob(editor) · carol(viewer) · dave(局外人)。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"email-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_DATA_ISOLATION", "true")

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import session as session_mod
    from stockmaster.plugins.auth.services import service, tokens

    session_mod.reset_engine()
    session_mod.init_db()
    tokens.reset_secret_cache()
    service.reset_throttle()

    from stockmaster.main import app

    with TestClient(app) as client:
        client.alice = _register(client, "alice", admin=True)  # type: ignore[attr-defined]
        client.bob = _register(client, "bob")  # type: ignore[attr-defined]
        client.carol = _register(client, "carol")  # type: ignore[attr-defined]
        client.dave = _register(client, "dave")  # type: ignore[attr-defined]
        yield client

    service.reset_throttle()
    session_mod.reset_engine()
    get_settings.cache_clear()


def _register(client: TestClient, username: str, *, admin: bool = False) -> dict:
    res = client.post("/api/v1/auth/register", json={"username": username, "password": STRONG})
    assert res.status_code in (200, 201), res.text
    headers = {"Authorization": f"Bearer {res.json()['token']}"}
    me = client.get("/api/v1/auth/me", headers=headers).json()
    if admin:
        assert me["role"] == "admin"
    return {"headers": headers, "id": me["id"], "username": username}


def _h(who: dict, ws_id: int | None = None) -> dict:
    headers = dict(who["headers"])
    if ws_id is not None:
        headers["X-Workspace-Id"] = str(ws_id)
    return headers


def _create_workspace(client: TestClient, who: dict, name: str = "投研小组") -> int:
    res = client.post("/api/v1/workspaces", headers=_h(who), json={"name": name})
    assert res.status_code == 201, res.text
    return int(res.json()["id"])


def _add_member(client: TestClient, who: dict, ws_id: int, username: str, role: str) -> None:
    res = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        headers=_h(who, ws_id),
        json={"username": username, "role": role},
    )
    assert res.status_code == 201, res.text


def _create_channel(client: TestClient, who: dict, ws_id: int, *, name: str, kind: str, target: str):
    return client.post(
        f"/api/v1/workspaces/{ws_id}/channels",
        headers=_h(who, ws_id),
        json={"name": name, "kind": kind, "url": target},
    )


def _channel_row(ws_id: int, channel_id: int):
    from stockmaster.db import session_scope
    from stockmaster.db.models import WorkspaceChannel

    with session_scope() as session:
        row = session.get(WorkspaceChannel, channel_id)
        assert row is not None and row.workspace_id == ws_id
        return row


def test_owner_creates_email_channel_and_masks_recipients(email_client):
    client = email_client
    ws_id = _create_workspace(client, client.alice)

    created = _create_channel(
        client,
        client.alice,
        ws_id,
        name="团队邮件",
        kind="email",
        target=f"{SECRET_EMAIL};  {SECOND_EMAIL}",
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "email"
    # 完整邮箱不出现在响应里；掩码保留首字母与域名
    assert SECRET_EMAIL not in created.text
    assert SECOND_EMAIL not in created.text
    assert body["url"] == "a***@example.com, b***@example.com"

    # 入库的是规范化后的收件人串（分号转逗号、去多余空格）
    assert _channel_row(ws_id, body["id"]).url == f"{SECRET_EMAIL}, {SECOND_EMAIL}"

    listed = client.get(f"/api/v1/workspaces/{ws_id}/channels", headers=_h(client.alice, ws_id))
    assert listed.status_code == 200
    assert SECRET_EMAIL not in listed.text
    assert listed.json()[0]["url"] == "a***@example.com, b***@example.com"


def test_invalid_email_channel_targets_are_rejected(email_client):
    client = email_client
    ws_id = _create_workspace(client, client.alice)

    invalid_targets = [
        "not-an-email",
        "a@b",  # 缺顶级域
        "a b@example.com",
        ", ;",
        "a@example.com, " + ", ".join(f"user{index}@example.com" for index in range(10)),  # 11 个
    ]
    for target in invalid_targets:
        res = _create_channel(client, client.alice, ws_id, name=f"坏地址{len(target)}", kind="email", target=target)
        assert res.status_code == 422, f"{target!r} 应被拒：{res.status_code} {res.text}"
        assert res.headers.get("X-Auth-Error") == "invalid_channel"

    # webhook 的 target 是 URL：传邮箱（或反之）必须 422
    assert (
        _create_channel(
            client, client.alice, ws_id, name="webhook 收件人", kind="webhook", target=SECRET_EMAIL
        ).status_code
        == 422
    )
    created = _create_channel(
        client, client.alice, ws_id, name="正常 webhook", kind="webhook", target="https://example.com/hook"
    )
    assert created.status_code == 201
    # kind 改成 email 但沿用 http URL → 422（不发散成「能建不能发」的通道）
    changed = client.patch(
        f"/api/v1/workspaces/{ws_id}/channels/{created.json()['id']}",
        headers=_h(client.alice, ws_id),
        json={"kind": "email"},
    )
    assert changed.status_code == 422


def test_email_channel_role_matrix(email_client):
    client = email_client
    ws_id = _create_workspace(client, client.alice)
    _create_channel(client, client.alice, ws_id, name="邮件", kind="email", target=SECRET_EMAIL)
    _add_member(client, client.alice, ws_id, "bob", "editor")
    _add_member(client, client.alice, ws_id, "carol", "viewer")

    for who in (client.bob, client.carol):
        res = _create_channel(client, who, ws_id, name="越权", kind="email", target=SECOND_EMAIL)
        assert res.status_code == 403
        assert res.headers.get("X-Auth-Error") == "forbidden"

    assert (
        _create_channel(client, client.dave, ws_id, name="局外人", kind="email", target=SECOND_EMAIL).status_code == 404
    )


def test_email_channel_test_endpoint_uses_smtp(email_client, smtp_config):
    client = email_client
    ws_id = _create_workspace(client, client.alice)
    channel_id = _create_channel(client, client.alice, ws_id, name="邮件", kind="email", target=SECRET_EMAIL).json()[
        "id"
    ]
    url = f"/api/v1/workspaces/{ws_id}/channels/{channel_id}/test"

    _FakeSMTP.created = []
    ok = client.post(url, headers=_h(client.alice))
    assert ok.status_code == 200
    assert ok.json()["delivered"] is True
    assert len(_FakeSMTP.created) == 1

    # 未配置 SMTP → 仍是 200 + delivered=false（不是 500）
    from stockmaster.config import settings

    settings.smtp_host = ""
    try:
        missing = client.post(url, headers=_h(client.alice))
        assert missing.status_code == 200
        assert missing.json()["delivered"] is False
    finally:
        settings.smtp_host = "smtp.example.com"


# --------------------------------------------------------------------------- #
# 工作区消息模板：保存与校验
# --------------------------------------------------------------------------- #


def test_workspace_message_template_saved_and_validated(email_client):
    client = email_client
    ws_id = _create_workspace(client, client.alice)
    template = "【{level}】{title}\n{body}（{code} #{rule_id}）"

    res = client.patch(f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice), json={"message_template": template})
    assert res.status_code == 200, res.text
    assert res.json()["message_template"] == template

    fetched = client.get(f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice)).json()
    assert fetched["message_template"] == template

    # 未知占位符 / 非法花括号 / 超长 → 422
    for bad in ("{nope}", "未闭合 {title", "多余 }"):
        invalid = client.patch(f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice), json={"message_template": bad})
        assert invalid.status_code == 422, f"{bad!r} 应被拒：{invalid.status_code} {invalid.text}"
        assert invalid.headers.get("X-Auth-Error") == "invalid_template"
    too_long = client.patch(
        f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice), json={"message_template": "x" * 2001}
    )
    assert too_long.status_code == 422

    # 允许格式说明符与转换符
    fancy = client.patch(
        f"/api/v1/workspaces/{ws_id}",
        headers=_h(client.alice),
        json={"message_template": "{title:>20} {body!r} {rule_id:04d}"},
    )
    assert fancy.status_code == 200, fancy.text

    # 空串 = 清除
    cleared = client.patch(f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice), json={"message_template": ""})
    assert cleared.status_code == 200
    assert cleared.json()["message_template"] == ""


# --------------------------------------------------------------------------- #
# notify() 套用工作区模板
# --------------------------------------------------------------------------- #


def _set_template(ws_id: int, template: str) -> None:
    from stockmaster.db import session_scope
    from stockmaster.db.models import Workspace

    with session_scope() as session:
        workspace = session.get(Workspace, ws_id)
        assert workspace is not None
        workspace.message_template = template


def _add_webhook_row(ws_id: int, url: str) -> None:
    from stockmaster.db import session_scope
    from stockmaster.db.models import WorkspaceChannel

    with session_scope() as session:
        session.add(WorkspaceChannel(workspace_id=ws_id, name=f"hook-{url}", kind="webhook", url=url, enabled=True))


@pytest.fixture
def outbound(monkeypatch):
    """记录 webhook 外发，替代真实网络。"""
    from stockmaster.plugins.alerts.services.channels import WebhookChannel

    sent: list[tuple[str, object]] = []

    def webhook_send(self, notification):
        sent.append((self.url, notification))
        return True

    monkeypatch.setattr(WebhookChannel, "send", webhook_send)
    return sent


def test_notify_applies_workspace_template(email_client, outbound, monkeypatch):
    from stockmaster.config import settings
    from stockmaster.plugins.alerts.services.channels import Notification, notify

    client = email_client
    ws_a = _create_workspace(client, client.alice, "A 组")
    ws_b = _create_workspace(client, client.alice, "B 组")
    _set_template(ws_a, "【{level}】{title} / {body} / {code} / {rule_id}")
    _add_webhook_row(ws_a, "https://a.example/hook")
    _add_webhook_row(ws_b, "https://b.example/hook")
    monkeypatch.setattr(settings, "data_isolation", True)
    monkeypatch.setattr(settings, "alert_webhook_url", "https://global.example/hook")

    results = notify(
        [
            Notification(
                title="A 预警", body="原始正文", code="600519.SH", rule_id=7, level="warning", workspace_id=ws_a
            ),
            Notification(title="B 预警", body="B 原文", code="000001.SZ", workspace_id=ws_b),
            Notification(title="未归属", body="0 原文", workspace_id=0),
        ]
    )

    delivered = dict(outbound)
    # 有模板：正文被重写，标题保持不变（模板里可自行嵌入 {title}）
    a = delivered["https://a.example/hook"]
    assert a.body == "【warning】A 预警 / 原始正文 / 600519.SH / 7"
    assert a.title == "A 预警"
    assert a.code == "600519.SH"
    # 无模板：原文不变
    assert delivered["https://b.example/hook"].body == "B 原文"
    # workspace 0 不套任何工作区模板（即使 A 配了）
    zero = [notification for url, notification in outbound if url == "https://global.example/hook"]
    assert len(zero) == 1 and zero[0].body == "0 原文"
    assert results[f"ws{ws_a}:webhook"] == 1
    assert results[f"ws{ws_b}:webhook"] == 1
    assert results["ws0:webhook"] == 1


def test_notify_falls_back_when_template_breaks_at_runtime(email_client, outbound, monkeypatch):
    """API 会拦截非法模板，但历史/手改数据可能坏掉：运行期失败回退原文而不是不发。"""
    from stockmaster.config import settings
    from stockmaster.plugins.alerts.services.channels import Notification, notify

    client = email_client
    ws_id = _create_workspace(client, client.alice)
    _set_template(ws_id, "{unknown_field}")  # 绕过 API 直接写库
    _add_webhook_row(ws_id, "https://broken.example/hook")
    monkeypatch.setattr(settings, "data_isolation", True)

    results = notify([Notification(title="标题", body="正文", workspace_id=ws_id)])

    assert results[f"ws{ws_id}:webhook"] == 1
    (_, delivered) = next((url, notification) for url, notification in outbound if url == "https://broken.example/hook")
    assert delivered.body == "正文"
