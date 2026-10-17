"""用户页数据量（``GET /workspaces`` 的 ``data_total``）与管理员用户统计列。

为什么单独一套用例
-----------------
这两个数字很容易「看起来对」却口径不一致：用户页说「我有一个空间、N 条数据」，
管理员列表却说「他参与 3 个工作区、个人空间 0 条」。所以这里除了断言字段存在，
还断言两组数字互相一致（``personal_data_total`` == personal 工作区的 ``data_total``），
并明确成员与通知通道**不**计入数据量。

为什么单独测 ``data_totals``
--------------------------
管理员列表是「每页 N 个用户 × 6 张表」的批量聚合，最怕为了图省事写成逐用户查询或
把不同工作区的计数串到一起；空输入、未知 id 补 0、去重这些边界直接对函数断言。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"


@pytest.fixture
def user_client(tmp_path, monkeypatch):
    """四个账号：alice(admin) · bob · carol · dave。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"user-stats-{uuid.uuid4().hex}.db")
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


def _create_workspace(client: TestClient, who: dict, name: str) -> int:
    res = client.post("/api/v1/workspaces", headers=_h(who), json={"name": name})
    assert res.status_code == 201, res.text
    return int(res.json()["id"])


def _personal_workspace(client: TestClient, who: dict) -> dict:
    rows = client.get("/api/v1/workspaces", headers=_h(who)).json()
    personal = [row for row in rows if row["kind"] == "personal"]
    assert len(personal) == 1, rows
    return personal[0]


def _add_watchlist(client: TestClient, who: dict, ws_id: int, code: str = "600519.SH") -> dict:
    res = client.post("/api/v1/watchlist", headers=_h(who, ws_id), json={"code": code})
    assert res.status_code == 201, res.text
    return res.json()


def _add_alert(client: TestClient, who: dict, ws_id: int, name: str = "规则") -> dict:
    res = client.post(
        "/api/v1/alerts/rules",
        headers=_h(who, ws_id),
        json={"name": name, "rule_type": "price_cross", "params": {"threshold": 100.0}},
    )
    assert res.status_code == 201, res.text
    return res.json()


def _users(client: TestClient, who: dict, **params) -> dict:
    res = client.get("/api/v1/users", headers=_h(who), params=params)
    assert res.status_code == 200, res.text
    return res.json()


def _user_row(client: TestClient, who: dict, username: str) -> dict:
    for row in _users(client, who)["items"]:
        if row["username"] == username:
            return row
    raise AssertionError(f"{username} 不在用户列表里")


# --------------------------------------------------------------------------- #
# 用户页：GET /workspaces 的 data_total
# --------------------------------------------------------------------------- #


def test_workspace_list_data_total_excludes_members_and_channels(user_client):
    client = user_client
    personal = _personal_workspace(client, client.bob)
    assert personal["data_total"] == 0
    ws_id = personal["id"]

    # 自选 + 预警规则计入数据量
    _add_watchlist(client, client.bob, ws_id)
    _add_alert(client, client.bob, ws_id)

    # 通知通道不计入（成员也不计入）
    channel = client.post(
        f"/api/v1/workspaces/{ws_id}/channels",
        headers=_h(client.bob),
        json={"name": "主群", "kind": "webhook", "url": "https://example.com/hook?token=secret5678"},
    )
    assert channel.status_code == 201, channel.text
    member = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        headers=_h(client.bob),
        json={"username": "alice", "role": "viewer"},
    )
    assert member.status_code == 201, member.text

    rows = client.get("/api/v1/workspaces", headers=_h(client.bob)).json()
    bob_personal = next(row for row in rows if row["id"] == ws_id)
    assert bob_personal["data_total"] == 2
    # 每个 workpace 项都带字段（前端无需兜底）
    assert all("data_total" in row for row in rows)

    # 详情（单条路径）同样带 data_total
    detail = client.get(f"/api/v1/workspaces/{ws_id}", headers=_h(client.bob)).json()
    assert detail["data_total"] == 2

    # 管理员 ?all=true 分支走批量统计，结果一致
    everything = client.get("/api/v1/workspaces?all=true", headers=_h(client.alice)).json()
    assert next(row for row in everything if row["id"] == ws_id)["data_total"] == 2

    # 新建工作区没有任何数据 → 0
    created = client.post("/api/v1/workspaces", headers=_h(client.alice), json={"name": "新工作区"})
    assert created.status_code == 201
    assert created.json()["data_total"] == 0


# --------------------------------------------------------------------------- #
# 管理员用户列表统计列
# --------------------------------------------------------------------------- #


def test_admin_user_stats_columns_and_shared_workspace(user_client):
    client = user_client
    body = _users(client, client.alice)
    items = body["items"]
    for item in items:
        assert {"workspace_count", "owned_workspace_count", "personal_data_total"} <= set(item)
        assert item["workspace_count"] >= 1  # 注册即拥有 personal 工作区

    bob = _user_row(client, client.alice, "bob")
    alice = _user_row(client, client.alice, "alice")
    assert (bob["workspace_count"], bob["owned_workspace_count"], bob["personal_data_total"]) == (1, 1, 0)
    assert (alice["workspace_count"], alice["owned_workspace_count"]) == (1, 1)

    # alice 建共享工作区并邀请 bob：两人的「参与工作区数」+1，bob 的 owned 不变
    shared_id = _create_workspace(client, client.alice, "团队")
    added = client.post(
        f"/api/v1/workspaces/{shared_id}/members",
        headers=_h(client.alice),
        json={"username": "bob", "role": "editor"},
    )
    assert added.status_code == 201, added.text

    # 给 bob 的个人空间放 1 条自选 + 1 条规则
    bob_personal = _personal_workspace(client, client.bob)
    _add_watchlist(client, client.bob, bob_personal["id"])
    _add_alert(client, client.bob, bob_personal["id"])

    bob = _user_row(client, client.alice, "bob")
    alice = _user_row(client, client.alice, "alice")
    assert bob["workspace_count"] == 2
    assert bob["owned_workspace_count"] == 1  # 共享工作区是 alice 的
    assert alice["workspace_count"] == 2
    assert alice["owned_workspace_count"] == 2
    assert bob["personal_data_total"] == 2

    # 与用户页同一口径：等于 personal 工作区的 data_total
    bob_rows = client.get("/api/v1/workspaces", headers=_h(client.bob)).json()
    personal_row = next(row for row in bob_rows if row["id"] == bob_personal["id"])
    assert personal_row["data_total"] == bob["personal_data_total"]

    # q 过滤后统计仍然正确（聚合不能依赖「整页用户」）
    filtered = client.get("/api/v1/users", headers=_h(client.alice), params={"q": "bob"})
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["total"] == 1
    row = filtered_body["items"][0]
    assert row["username"] == "bob"
    assert (row["workspace_count"], row["owned_workspace_count"], row["personal_data_total"]) == (2, 1, 2)

    # 空结果不报错
    empty = client.get("/api/v1/users", headers=_h(client.alice), params={"q": "不存在的人"})
    assert empty.status_code == 200
    assert empty.json() == {"total": 0, "items": []}


def test_non_admin_cannot_list_users(user_client):
    client = user_client
    for who in (client.bob, client.carol, client.dave):
        res = client.get("/api/v1/users", headers=_h(who))
        assert res.status_code == 403, f"{who['username']} 不应能读用户列表：{res.status_code}"


# --------------------------------------------------------------------------- #
# data_totals 批量聚合的边界
# --------------------------------------------------------------------------- #


def test_data_totals_batch_and_workspace_isolation(user_client):
    client = user_client
    ws_a = _create_workspace(client, client.alice, "A 组")
    ws_b = _create_workspace(client, client.alice, "B 组")
    _add_watchlist(client, client.alice, ws_a, "600519.SH")
    _add_watchlist(client, client.alice, ws_a, "000001.SZ")
    _add_alert(client, client.alice, ws_b, "B 组规则")

    from stockmaster.core import quota
    from stockmaster.db import session_scope

    with session_scope() as session:
        assert quota.data_totals(session, []) == {}
        # 各工作区只算自己的数据，不串
        assert quota.data_totals(session, [ws_a, ws_b]) == {ws_a: 2, ws_b: 1}
        # 重复 id 去重
        assert quota.data_totals(session, [ws_a, ws_a]) == {ws_a: 2}
        # 未知 id 补 0
        assert quota.data_totals(session, [ws_a, 999999]) == {ws_a: 2, 999999: 0}
        # 单条版与批量版一致
        assert quota.data_total(session, ws_a) == 2
        assert quota.data_total(session, 0) == 0
        assert quota.data_total(session, 999999) == 0
