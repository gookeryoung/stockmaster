"""工作区级通知通道：CRUD、URL 掩码、角色矩阵与测试投递。

为什么单独一套 fixture
---------------------
通道是**数据出口**，写权限只给 owner（editor 能改通道等于允许他把本工作区数据转发到
任意地址），所以这里同样要跑 owner / editor / viewer / 局外人四种身份。fixture 与
``test_workspace.py`` 保持一致，但独立一份：两个测试文件互不 import，避免一个文件
的 fixture 改动牵连另一个文件的角色矩阵。

URL 为什么必须断言「不含明文」
-----------------------------
webhook token、Bark device key 都在 URL 里。接口一旦回显明文，任何 viewer 都能把工作区
的通知转发到自己那里 —— 这是隔离在出口处的最后一道门。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"
SECRET = "verysecretTOKEN1234"
WEBHOOK_URL = f"https://oapi.dingtalk.com/robot/send?access_token={SECRET}"
BARK_URL = "https://api.day.app/DEVICEKEY5678"


@pytest.fixture
def channels_client(tmp_path, monkeypatch):
    """四个账号：alice(admin/owner) · bob(editor) · carol(viewer) · dave(局外人)。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"channels-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_DATA_ISOLATION", "true")
    monkeypatch.setenv("SM_ALERT_WEBHOOK_URL", "")
    monkeypatch.setenv("SM_ALERT_GLOBAL_FALLBACK", "false")

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
        headers=_h(who),
        json={"username": username, "role": role},
    )
    assert res.status_code == 201, res.text


def _create_channel(client: TestClient, who: dict, ws_id: int, **overrides) -> dict:
    payload = {"name": "主群", "kind": "webhook", "url": WEBHOOK_URL, "enabled": True}
    payload.update(overrides)
    return client.post(f"/api/v1/workspaces/{ws_id}/channels", headers=_h(who), json=payload)


def _list_channels(client: TestClient, who: dict, ws_id: int) -> list[dict]:
    res = client.get(f"/api/v1/workspaces/{ws_id}/channels", headers=_h(who))
    assert res.status_code == 200, res.text
    return res.json()


def _db_channel(ws_id: int, channel_id: int) -> tuple[str, str, str, bool] | None:
    """直接读库，验证「URL 保持原值」这类掩码接口看不到的事实。"""
    from stockmaster.db import session_scope
    from stockmaster.db.models import WorkspaceChannel

    with session_scope() as session:
        row = session.get(WorkspaceChannel, channel_id)
        if row is None or row.workspace_id != ws_id:
            return None
        return (row.name, row.kind, row.url, row.enabled)


def _db_channel_count(ws_id: int) -> int:
    from stockmaster.db import session_scope
    from stockmaster.db.models import WorkspaceChannel

    with session_scope() as session:
        return session.query(WorkspaceChannel).filter(WorkspaceChannel.workspace_id == ws_id).count()


# --------------------------------------------------------------------------- #
# mask_url：单元级保证「绝不回显 query/path 中的密钥」
# --------------------------------------------------------------------------- #


def test_mask_url_never_reveals_secret():
    from stockmaster.plugins.workspaces.services.service import mask_url

    masked = mask_url(WEBHOOK_URL)
    assert SECRET not in masked
    assert "access_token" not in masked
    assert masked.startswith("https://oapi.dingtalk.com/")
    assert "****" in masked
    # 保留最后 4 位，方便成员区分「同名不同 token」的两条通道
    assert masked.endswith(WEBHOOK_URL[-4:])

    # 无法解析 / 过短 / 非 http(s) 一律整体打码，而不是回显
    assert mask_url("") == "****"
    assert mask_url("abc") == "****"
    assert mask_url("ftp://example.com/token") == "****"
    assert mask_url("https://") == "****"


# --------------------------------------------------------------------------- #
# owner CRUD
# --------------------------------------------------------------------------- #


def test_owner_creates_and_lists_masked_channel(channels_client):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)

    created = _create_channel(client, client.alice, ws_id)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "主群"
    assert body["kind"] == "webhook"
    assert body["enabled"] is True
    assert isinstance(body["id"], int)
    # 响应里只有掩码，明文 token 不出现
    assert body["url"] != WEBHOOK_URL
    assert SECRET not in created.text
    assert "****" in body["url"]

    rows = _list_channels(client, client.alice, ws_id)
    assert [row["id"] for row in rows] == [body["id"]]
    listed = client.get(f"/api/v1/workspaces/{ws_id}/channels", headers=_h(client.alice))
    assert SECRET not in listed.text
    assert "****" in rows[0]["url"]

    # 库里仍然存明文（投递时要用），只有接口脱敏
    assert _db_channel(ws_id, body["id"]) == ("主群", "webhook", WEBHOOK_URL, True)

    # 成员（editor / viewer）都能读列表，但同样只看得到掩码
    _add_member(client, client.alice, ws_id, "bob", "editor")
    _add_member(client, client.alice, ws_id, "carol", "viewer")
    for who in (client.bob, client.carol):
        res = client.get(f"/api/v1/workspaces/{ws_id}/channels", headers=_h(who, ws_id))
        assert res.status_code == 200
        assert SECRET not in res.text


def test_owner_updates_channel_without_touching_url(channels_client):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)
    channel_id = _create_channel(client, client.alice, ws_id).json()["id"]

    # 改名 + 停用，省略 url：原地址必须原样保留
    res = client.patch(
        f"/api/v1/workspaces/{ws_id}/channels/{channel_id}",
        headers=_h(client.alice),
        json={"name": "备用群", "enabled": False},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "备用群"
    assert body["enabled"] is False
    assert body["kind"] == "webhook"
    assert SECRET not in res.text
    assert _db_channel(ws_id, channel_id) == ("备用群", "webhook", WEBHOOK_URL, False)

    # 显式传 null 也等于「不动 url」
    assert (
        client.patch(
            f"/api/v1/workspaces/{ws_id}/channels/{channel_id}",
            headers=_h(client.alice),
            json={"url": None},
        ).status_code
        == 200
    )
    assert _db_channel(ws_id, channel_id)[2] == WEBHOOK_URL

    # 给新地址才替换；改 kind 也生效
    res = client.patch(
        f"/api/v1/workspaces/{ws_id}/channels/{channel_id}",
        headers=_h(client.alice),
        json={"url": BARK_URL, "kind": "bark"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["kind"] == "bark"
    assert _db_channel(ws_id, channel_id) == ("备用群", "bark", BARK_URL, False)


def test_owner_deletes_channel(channels_client):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)
    channel_id = _create_channel(client, client.alice, ws_id).json()["id"]
    second_id = _create_channel(client, client.alice, ws_id, name="第二条").json()["id"]
    # 列表按 id 升序
    assert [row["id"] for row in _list_channels(client, client.alice, ws_id)] == [channel_id, second_id]

    deleted = client.delete(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}", headers=_h(client.alice))
    assert deleted.status_code == 204
    assert _db_channel(ws_id, channel_id) is None
    assert [row["id"] for row in _list_channels(client, client.alice, ws_id)] == [second_id]

    # 再删一次 → 404
    assert (
        client.delete(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}", headers=_h(client.alice)).status_code == 404
    )


# --------------------------------------------------------------------------- #
# 校验与唯一性
# --------------------------------------------------------------------------- #


def test_channel_validation(channels_client):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)

    invalid_payloads = [
        {"name": "x", "kind": "carrier_pigeon", "url": WEBHOOK_URL},  # 未知类型
        {"name": "x", "kind": "webhook", "url": "ftp://example.com/hook"},  # 非 http(s)
        {"name": "x", "kind": "webhook", "url": "not-a-url"},
        {"name": "x", "kind": "webhook", "url": "https://example.com/a b"},  # 空白
        {"name": "", "kind": "webhook", "url": WEBHOOK_URL},  # 空名
        {"name": "   ", "kind": "webhook", "url": WEBHOOK_URL},  # 纯空白名
        {"name": "x" * 65, "kind": "webhook", "url": WEBHOOK_URL},  # 名字超长
        {"kind": "webhook", "url": WEBHOOK_URL},  # 缺 name
        {"name": "x", "kind": "webhook"},  # 缺 url
    ]
    for payload in invalid_payloads:
        res = client.post(f"/api/v1/workspaces/{ws_id}/channels", headers=_h(client.alice), json=payload)
        assert res.status_code == 422, f"{payload} 应被拒：{res.status_code} {res.text}"

    assert _list_channels(client, client.alice, ws_id) == []


def test_duplicate_name_is_conflict_but_other_workspace_is_fine(channels_client):
    client = channels_client
    ws_a = _create_workspace(client, client.alice, name="A 组")
    ws_b = _create_workspace(client, client.alice, name="B 组")

    assert _create_channel(client, client.alice, ws_a, name="预警群").status_code == 201
    duplicate = _create_channel(client, client.alice, ws_a, name="预警群")
    assert duplicate.status_code == 409
    assert duplicate.headers.get("X-Auth-Error") == "duplicate_channel"
    assert "预警群" in duplicate.json()["detail"]

    # 同一名字在别的工作区完全合法
    assert _create_channel(client, client.alice, ws_b, name="预警群").status_code == 201

    # PATCH 改成已存在的名字同样 409；改成自己原来的名字是 no-op
    channel_id = _create_channel(client, client.alice, ws_a, name="第二个").json()["id"]
    clash = client.patch(
        f"/api/v1/workspaces/{ws_a}/channels/{channel_id}",
        headers=_h(client.alice),
        json={"name": "预警群"},
    )
    assert clash.status_code == 409
    assert clash.headers.get("X-Auth-Error") == "duplicate_channel"
    same = client.patch(
        f"/api/v1/workspaces/{ws_a}/channels/{channel_id}",
        headers=_h(client.alice),
        json={"name": "第二个"},
    )
    assert same.status_code == 200


# --------------------------------------------------------------------------- #
# 角色矩阵：通道只给 owner 写
# --------------------------------------------------------------------------- #


def test_only_owner_can_write_channels(channels_client):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)
    channel_id = _create_channel(client, client.alice, ws_id).json()["id"]
    _add_member(client, client.alice, ws_id, "bob", "editor")
    _add_member(client, client.alice, ws_id, "carol", "viewer")

    for who in (client.bob, client.carol):
        headers = _h(who, ws_id)
        # 读列表可以
        assert client.get(f"/api/v1/workspaces/{ws_id}/channels", headers=headers).status_code == 200
        # 每一个写端点都必须是 403
        writes = [
            client.post(
                f"/api/v1/workspaces/{ws_id}/channels",
                headers=headers,
                json={"name": "越权", "kind": "webhook", "url": WEBHOOK_URL},
            ),
            client.patch(
                f"/api/v1/workspaces/{ws_id}/channels/{channel_id}",
                headers=headers,
                json={"name": "越权"},
            ),
            client.delete(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}", headers=headers),
            client.post(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}/test", headers=headers),
        ]
        for res in writes:
            assert res.status_code == 403, f"{who['username']} {res.request.method} 应 403：{res.text}"
            assert res.headers.get("X-Auth-Error") == "forbidden"

    # 越权尝试没有留下任何改动
    assert _db_channel(ws_id, channel_id) == ("主群", "webhook", WEBHOOK_URL, True)


def test_non_member_and_missing_ids_are_404(channels_client):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)
    channel_id = _create_channel(client, client.alice, ws_id).json()["id"]

    # 局外人：连列表都看不到（404 而不是 403，不泄露工作区存在性）
    dave = _h(client.dave, ws_id)
    assert client.get(f"/api/v1/workspaces/{ws_id}/channels", headers=dave).status_code == 404
    assert (
        client.post(
            f"/api/v1/workspaces/{ws_id}/channels",
            headers=dave,
            json={"name": "x", "kind": "webhook", "url": WEBHOOK_URL},
        ).status_code
        == 404
    )
    for res in (
        client.patch(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}", headers=dave, json={"name": "x"}),
        client.delete(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}", headers=dave),
        client.post(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}/test", headers=dave),
    ):
        assert res.status_code == 404

    # 不存在的通道 / 不存在的工作区
    alice = _h(client.alice)
    assert (
        client.patch(f"/api/v1/workspaces/{ws_id}/channels/99999", headers=alice, json={"name": "x"}).status_code == 404
    )
    assert client.delete(f"/api/v1/workspaces/{ws_id}/channels/99999", headers=alice).status_code == 404
    assert client.post(f"/api/v1/workspaces/{ws_id}/channels/99999/test", headers=alice).status_code == 404
    assert client.get("/api/v1/workspaces/99999/channels", headers=alice).status_code == 404


def test_channel_of_another_workspace_is_404(channels_client):
    client = channels_client
    ws_a = _create_workspace(client, client.alice, name="A 组")
    ws_b = _create_workspace(client, client.alice, name="B 组")
    channel_in_b = _create_channel(client, client.alice, ws_b, name="B 的通道").json()["id"]

    alice = _h(client.alice)
    # 用 A 的路径访问 B 的 channel_id：一律 404（与其他私有资源同一约定）
    assert (
        client.patch(
            f"/api/v1/workspaces/{ws_a}/channels/{channel_in_b}", headers=alice, json={"name": "x"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/v1/workspaces/{ws_a}/channels/{channel_in_b}", headers=alice).status_code == 404
    assert client.post(f"/api/v1/workspaces/{ws_a}/channels/{channel_in_b}/test", headers=alice).status_code == 404
    # A 的列表里也不会出现 B 的通道
    assert _list_channels(client, client.alice, ws_a) == []


# --------------------------------------------------------------------------- #
# 测试投递：网络失败不 500
# --------------------------------------------------------------------------- #


def _patch_send_test(monkeypatch, outcome):
    """把投递实现换成替身；``outcome`` 为异常实例时模拟网络故障。

    端点现在走 ``send_test_detail``（返回 ``(delivered, reason)``），替身也返回
    二元组；``send_test`` 仍保留为兼容包装。
    """
    channels = pytest.importorskip("stockmaster.plugins.alerts.services.channels")
    calls: list = []

    def fake_send_test_detail(row):
        calls.append(row)
        if isinstance(outcome, Exception):
            raise outcome
        return (outcome, "" if outcome else "delivery_failed")

    monkeypatch.setattr(channels, "send_test_detail", fake_send_test_detail, raising=False)
    return calls


def test_owner_can_send_test_notification(channels_client, monkeypatch):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)
    channel_id = _create_channel(client, client.alice, ws_id, name="测试群").json()["id"]
    calls = _patch_send_test(monkeypatch, True)

    res = client.post(f"/api/v1/workspaces/{ws_id}/channels/{channel_id}/test", headers=_h(client.alice))
    assert res.status_code == 200, res.text
    assert res.json() == {"delivered": True, "channel": "测试群", "kind": "webhook"}
    # 投递拿到的是带明文 URL 的 ORM 行（掩码只作用于 HTTP 响应）
    assert len(calls) == 1 and calls[0].id == channel_id and calls[0].url == WEBHOOK_URL


def test_test_endpoint_reports_failure_instead_of_500(channels_client, monkeypatch):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)
    channel_id = _create_channel(client, client.alice, ws_id).json()["id"]
    url = f"/api/v1/workspaces/{ws_id}/channels/{channel_id}/test"

    # 投递函数返回 False（地址错、对端 4xx/5xx、超时由实现内部吞掉）
    _patch_send_test(monkeypatch, False)
    res = client.post(url, headers=_h(client.alice))
    assert res.status_code == 200
    assert res.json()["delivered"] is False
    assert res.json()["reason"] == "delivery_failed"

    # 投递函数意外抛异常：接口仍然 200 + delivered=false，而不是 500
    _patch_send_test(monkeypatch, RuntimeError("network down"))
    res = client.post(url, headers=_h(client.alice))
    assert res.status_code == 200
    assert res.json()["delivered"] is False
    assert res.json()["reason"] == "exception"


# --------------------------------------------------------------------------- #
# 删除工作区：通道随工作区消失，但不影响 purge 计数语义
# --------------------------------------------------------------------------- #


def test_deleting_workspace_removes_channels_without_counting_them(channels_client):
    client = channels_client
    ws_id = _create_workspace(client, client.alice)
    _create_channel(client, client.alice, ws_id, name="一条")
    _create_channel(client, client.alice, ws_id, name="两条")
    assert _db_channel_count(ws_id) == 2

    # 通道不是业务数据，不应触发「工作区非空，需要 purge」；purged 里也没有它
    res = client.delete(f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice))
    assert res.status_code == 200, res.text
    purged = res.json()["purged"]
    assert all(value == 0 for value in purged.values()), purged
    assert "workspace_channels" not in purged
    assert _db_channel_count(ws_id) == 0

    from stockmaster.db import session_scope
    from stockmaster.db.models import Workspace

    with session_scope() as session:
        assert session.get(Workspace, ws_id) is None
