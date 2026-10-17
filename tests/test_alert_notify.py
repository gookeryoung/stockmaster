"""工作区通知路由测试。

为什么要有这一套
----------------
隔离做到通知出口时最容易出的问题不是「发不出去」，而是**发错工作区**：A 组的股票
代码、阈值和触发消息出现在 B 组的群机器人里。这里会真的往库里写工作区与通道行，
再把网络外发全部打桩记录，断言每个 URL 只收到自己工作区的通知。

同时固定隔离关闭时的行为：key 仍是 ``database``/``webhook``、所有通知都走全局
webhook、且**不打开任何 DB session** —— 这是开关可以随时回退、旧单测保持快速无
副作用的前提。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from stockmaster.config import settings
from stockmaster.db import session_scope
from stockmaster.db.models import Workspace, WorkspaceChannel
from stockmaster.plugins.alerts.services.channels import (
    BarkChannel,
    EmailChannel,
    Notification,
    WebhookChannel,
    build_channel,
    notify,
    send_test,
)

# --------------------------------------------------------------------------- #
# 数据库与打桩助手
# --------------------------------------------------------------------------- #


def _add_workspace(name: str) -> int:
    with session_scope() as session:
        workspace = Workspace(name=name)
        session.add(workspace)
        session.flush()
        return int(workspace.id)


def _add_channel(
    workspace_id: int,
    *,
    name: str,
    url: str,
    kind: str = "webhook",
    enabled: bool = True,
) -> int:
    with session_scope() as session:
        row = WorkspaceChannel(
            workspace_id=workspace_id,
            name=name,
            kind=kind,
            url=url,
            enabled=enabled,
        )
        session.add(row)
        session.flush()
        return int(row.id)


@pytest.fixture
def outbound(monkeypatch):
    """记录每一次外发，替代真实网络请求。"""
    sent: list[tuple[str, str, Notification]] = []

    def webhook_send(self, notification: Notification) -> bool:
        sent.append((self.url, "webhook", notification))
        return True

    def bark_send(self, notification: Notification) -> bool:
        sent.append((self.base_url, "bark", notification))
        return True

    monkeypatch.setattr(WebhookChannel, "send", webhook_send)
    monkeypatch.setattr(BarkChannel, "send", bark_send)
    return sent


# --------------------------------------------------------------------------- #
# 隔离关闭：旧行为逐字节不变
# --------------------------------------------------------------------------- #


class TestIsolationOff:
    def test_global_keys_and_all_notifications(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", False)
        monkeypatch.setattr(settings, "alert_webhook_url", "https://global.example/hook")

        results = notify(
            [
                Notification(title="a", body="x", code="600519.SH", workspace_id=1),
                Notification(title="b", body="y", code="000001.SZ", workspace_id=2),
            ]
        )

        # workspace_id 不参与路由：两条都走同一个全局 webhook，key 保持旧名字。
        assert results == {"database": 2, "webhook": 2}
        assert [url for url, _, _ in outbound] == ["https://global.example/hook"] * 2

    def test_database_only_when_no_webhook_configured(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", False)
        monkeypatch.setattr(settings, "alert_webhook_url", "")

        results = notify([Notification(title="t", body="b")])

        assert results == {"database": 1}
        assert outbound == []

    def test_no_db_session_is_opened(self, temp_db, monkeypatch):
        """隔离关闭时 notify 不能摸数据库：旧单测没有 temp_db 也必须能跑。"""
        monkeypatch.setattr(settings, "data_isolation", False)
        import stockmaster.db.session as session_mod

        def explode():
            raise AssertionError("notify() must not open a DB session when isolation is off")

        monkeypatch.setattr(session_mod, "get_session_factory", explode)

        results = notify([Notification(title="t", body="b")])

        assert results["database"] == 1


# --------------------------------------------------------------------------- #
# 隔离开启：按工作区路由
# --------------------------------------------------------------------------- #


class TestWorkspaceRouting:
    def test_each_url_receives_only_its_workspace(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", True)
        ws_a, ws_b = _add_workspace("A 组"), _add_workspace("B 组")
        _add_channel(ws_a, name="a", url="https://a.example/hook")
        _add_channel(ws_b, name="b", url="https://b.example/hook")

        results = notify(
            [
                Notification(title="A", body="a", code="600519.SH", workspace_id=ws_a),
                Notification(title="B", body="b", code="000001.SZ", workspace_id=ws_b),
                Notification(title="A2", body="a2", code="601318.SH", workspace_id=ws_a),
            ]
        )

        assert results == {
            f"ws{ws_a}:database": 2,
            f"ws{ws_a}:webhook": 2,
            f"ws{ws_b}:database": 1,
            f"ws{ws_b}:webhook": 1,
        }
        delivered_a = [n.code for url, _, n in outbound if url == "https://a.example/hook"]
        delivered_b = [n.code for url, _, n in outbound if url == "https://b.example/hook"]
        assert delivered_a == ["600519.SH", "601318.SH"]
        assert delivered_b == ["000001.SZ"]

    def test_bark_channel_is_routed_by_kind(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", True)
        ws = _add_workspace("手机推送")
        _add_channel(ws, name="phone", kind="bark", url="https://bark.example/key")

        results = notify([Notification(title="t", body="b", workspace_id=ws)])

        assert results == {f"ws{ws}:database": 1, f"ws{ws}:bark": 1}
        assert [url for url, _, _ in outbound] == ["https://bark.example/key"]
        assert outbound[0][1] == "bark"

    def test_multiple_channels_in_one_workspace(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", True)
        ws = _add_workspace("双通道")
        _add_channel(ws, name="hook", url="https://hook.example/a")
        _add_channel(ws, name="phone", kind="bark", url="https://bark.example/b")

        results = notify([Notification(title="t", body="b", workspace_id=ws)])

        assert results == {
            f"ws{ws}:database": 1,
            f"ws{ws}:webhook": 1,
            f"ws{ws}:bark": 1,
        }
        assert sorted(url for url, _, _ in outbound) == ["https://bark.example/b", "https://hook.example/a"]


# --------------------------------------------------------------------------- #
# 停用与兜底
# --------------------------------------------------------------------------- #


class TestDisabledAndFallback:
    def test_disabled_channel_is_not_used(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", True)
        ws = _add_workspace("停用通道")
        _add_channel(ws, name="off", url="https://off.example/hook", enabled=False)

        results = notify([Notification(title="t", body="b", workspace_id=ws)])

        assert results == {f"ws{ws}:database": 1}
        assert outbound == []

    def test_no_channel_without_fallback_does_not_send(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", True)
        monkeypatch.setattr(settings, "alert_global_fallback", False)
        monkeypatch.setattr(settings, "alert_webhook_url", "https://global.example/hook")
        ws = _add_workspace("无通道")

        results = notify([Notification(title="t", body="b", workspace_id=ws)])

        # 严格隔离：数据库里仍留了触发记录，但工作区不外发。
        assert results == {f"ws{ws}:database": 1}
        assert outbound == []

    def test_fallback_sends_to_global_webhook(self, temp_db, monkeypatch, outbound):
        monkeypatch.setattr(settings, "data_isolation", True)
        monkeypatch.setattr(settings, "alert_global_fallback", True)
        monkeypatch.setattr(settings, "alert_webhook_url", "https://global.example/hook")
        ws = _add_workspace("过渡期")

        results = notify([Notification(title="t", body="b", workspace_id=ws)])

        assert results == {f"ws{ws}:database": 1, f"ws{ws}:webhook": 1}
        assert [url for url, _, _ in outbound] == ["https://global.example/hook"]

    def test_fallback_does_not_duplicate_database_or_override_channels(self, temp_db, monkeypatch, outbound):
        """已有启用通道的工作区不应再收到全局 webhook。"""
        monkeypatch.setattr(settings, "data_isolation", True)
        monkeypatch.setattr(settings, "alert_global_fallback", True)
        monkeypatch.setattr(settings, "alert_webhook_url", "https://global.example/hook")
        ws = _add_workspace("有通道")
        _add_channel(ws, name="own", url="https://own.example/hook")

        results = notify([Notification(title="t", body="b", workspace_id=ws)])

        assert results == {f"ws{ws}:database": 1, f"ws{ws}:webhook": 1}
        assert [url for url, _, _ in outbound] == ["https://own.example/hook"]

    def test_unassigned_notification_uses_global_webhook(self, temp_db, monkeypatch, outbound):
        # workspace_id=0 是隔离上线前的历史数据，不属于任何工作区，仍走全局通道。
        monkeypatch.setattr(settings, "data_isolation", True)
        monkeypatch.setattr(settings, "alert_global_fallback", False)
        monkeypatch.setattr(settings, "alert_webhook_url", "https://global.example/hook")

        results = notify([Notification(title="t", body="b", code="600519.SH", workspace_id=0)])

        assert results == {"ws0:database": 1, "ws0:webhook": 1}
        assert [url for url, _, _ in outbound] == ["https://global.example/hook"]


# --------------------------------------------------------------------------- #
# 预警扫描任务：workspace_id 透传
# --------------------------------------------------------------------------- #


class TestJobScanAlerts:
    def test_carries_workspace_id_into_notification(self, monkeypatch):
        from stockmaster.jobs import tasks
        from stockmaster.plugins.alerts.services.engine import TriggerCandidate

        candidate = TriggerCandidate(
            rule_id=7,
            code="600519.SH",
            name="测试",
            trade_date=date(2026, 1, 5),
            rule_type="price_cross",
            message="收盘 100.00 上穿 90.00",
            snapshot={"close": 100.0, "threshold": 90.0},
            dedup_key="7:600519.SH:2026-01-05",
            workspace_id=42,
        )
        monkeypatch.setattr(tasks.AlertEngine, "evaluate_all", lambda self, **kwargs: [candidate])
        captured: list[Notification] = []

        def fake_notify(notifications: list[Notification]) -> dict[str, int]:
            captured.extend(notifications)
            return {"database": len(notifications)}

        monkeypatch.setattr(tasks, "notify", fake_notify)

        report = tasks.job_scan_alerts()

        assert report["triggered"] == 1
        assert report["delivered"] == {"database": 1}
        assert len(captured) == 1
        assert captured[0].workspace_id == 42
        assert captured[0].code == "600519.SH"
        assert captured[0].rule_id == 7


# --------------------------------------------------------------------------- #
# 通道构造与测试消息
# --------------------------------------------------------------------------- #


class TestBuildChannelAndSendTest:
    def test_build_channel_maps_known_kinds(self):
        webhook = build_channel(SimpleNamespace(kind="webhook", url="https://hook.example/a"))
        bark = build_channel(SimpleNamespace(kind="bark", url="https://bark.example/b"))
        email = build_channel(SimpleNamespace(kind="email", url="a@example.com, b@example.com"))

        assert isinstance(webhook, WebhookChannel)
        assert webhook.url == "https://hook.example/a"
        assert isinstance(bark, BarkChannel)
        assert bark.base_url == "https://bark.example/b"
        # email 的「地址」是收件人串，不是 URL
        assert isinstance(email, EmailChannel)
        assert email.recipients == ["a@example.com", "b@example.com"]

    def test_build_channel_unknown_kind_returns_none(self):
        assert build_channel(SimpleNamespace(kind="carrier_pigeon", url="https://mail.example")) is None
        assert build_channel(SimpleNamespace(kind="", url="https://hook.example")) is None

    def test_send_test_unknown_kind_returns_false(self):
        row = SimpleNamespace(kind="carrier_pigeon", url="https://mail.example", name="信鸽", workspace_id=3)

        assert send_test(row) is False

    def test_send_test_email_without_smtp_config_returns_false(self, monkeypatch):
        """email 已知，但部署没有配置 SM_SMTP_HOST：返回 False 而不是抛异常。"""
        from stockmaster.config import settings

        monkeypatch.setattr(settings, "smtp_host", "")
        row = SimpleNamespace(kind="email", url="a@example.com", name="邮件", workspace_id=3)

        assert send_test(row) is False

    def test_send_test_empty_url_returns_false_without_network(self, monkeypatch):
        def no_network(*args, **kwargs):
            raise AssertionError("empty URL must not reach the network")

        import httpx

        monkeypatch.setattr(httpx, "post", no_network)
        row = SimpleNamespace(kind="webhook", url="", name="空地址", workspace_id=3)

        assert send_test(row) is False

    def test_send_test_delivers_and_returns_true(self, monkeypatch, outbound):
        row = SimpleNamespace(kind="webhook", url="https://hook.example/test", name="测试钩子", workspace_id=3)

        assert send_test(row) is True
        assert [url for url, _, _ in outbound] == ["https://hook.example/test"]
        assert outbound[0][2].workspace_id == 3

    def test_send_test_swallows_delivery_exceptions(self, monkeypatch):
        def boom(self, notification):
            raise RuntimeError("network down")

        monkeypatch.setattr(WebhookChannel, "send", boom)
        row = SimpleNamespace(kind="webhook", url="https://hook.example/test", name="测试钩子", workspace_id=3)

        assert send_test(row) is False
