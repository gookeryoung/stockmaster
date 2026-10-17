"""工作区角色矩阵：owner / editor / viewer 能做什么、不能做什么。

为什么要有这一套
----------------
角色最容易出的问题不是「判断写错」，而是「某个写端点忘了加 ``require_editor``」。
这类漏洞在权限收紧之后不会自己暴露 —— 直到有人发现 viewer 能改数据。所以这里按
**角色 × 动作** 穷举：viewer 对每一个写端点都必须 403，editor 对成员管理必须 403，
owner 才能管理成员。

为什么用 404 而不是 403 表达「不是成员」
--------------------------------------
非成员连工作区存在与否都不该知道，否则可以靠状态码扫出别人建了多少工作区、
分别叫什么。成员之间才用 403（他知道工作区存在，只是权限不够）。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"


@pytest.fixture
def ws_client(tmp_path, monkeypatch):
    """四个账号：alice(admin/owner) · bob(editor) · carol(viewer) · dave(局外人)。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"ws-{uuid.uuid4().hex}.db")
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
    body = res.json()
    assert body["role"] == "owner"
    return int(body["id"])


def _add(client: TestClient, who: dict, ws_id: int, username: str, role: str) -> dict:
    res = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        headers=_h(who),
        json={"username": username, "role": role},
    )
    return {"status": res.status_code, "body": res.json() if res.content else {}}


def _member(client: TestClient, who: dict, ws_id: int, user_id: int) -> dict:
    rows = client.get(f"/api/v1/workspaces/{ws_id}/members", headers=_h(who)).json()
    for row in rows:
        if row["user_id"] == user_id:
            return row
    raise AssertionError(f"user {user_id} 不在成员列表里：{rows}")


# --------------------------------------------------------------------------- #
# 基础：个人工作区与角色等级
# --------------------------------------------------------------------------- #


def test_each_account_gets_a_personal_workspace(ws_client):
    client = ws_client
    for who in (client.alice, client.bob, client.carol):
        rows = client.get("/api/v1/workspaces", headers=_h(who)).json()
        personal = [row for row in rows if row["kind"] == "personal"]
        assert len(personal) == 1
        assert personal[0]["role"] == "owner"
        assert personal[0]["member_count"] == 1


def test_role_rank_ordering():
    from stockmaster.plugins.workspaces.services.roles import ROLE_RANK, WorkspaceRole, at_least, can_manage, can_write

    assert ROLE_RANK[WorkspaceRole.OWNER] > ROLE_RANK[WorkspaceRole.EDITOR] > ROLE_RANK[WorkspaceRole.VIEWER]
    assert can_write("owner") and can_write("editor") and not can_write("viewer")
    assert can_manage("owner") and not can_manage("editor") and not can_manage("viewer")
    # 未知角色必须按最低权限处理，任何 >= 比较都失败（拒绝优先）
    assert at_least("superuser", "viewer") is False
    assert at_least(None, "viewer") is False


# --------------------------------------------------------------------------- #
# viewer：只读
# --------------------------------------------------------------------------- #


def test_viewer_is_read_only(ws_client):
    client = ws_client
    ws_id = _create_workspace(client, client.alice)
    assert _add(client, client.alice, ws_id, "carol", "viewer")["status"] == 201

    h = _h(client.carol, ws_id)
    assert client.get("/api/v1/watchlist", headers=h).status_code == 200
    assert client.get("/api/v1/alerts/rules", headers=h).status_code == 200
    assert client.get("/api/v1/workspaces", headers=h).status_code == 200

    # 每一个写端点都必须挡住
    write_attempts = [
        client.post("/api/v1/watchlist", headers=h, json={"code": "600519.SH"}),
        client.post(
            "/api/v1/alerts/rules",
            headers=h,
            json={"name": "x", "rule_type": "price_cross", "params": {"threshold": 1}},
        ),
        client.post("/api/v1/alerts/scan", headers=h),
        client.post("/api/v1/backtest/run", headers=h, json={"strategy": "buy_hold", "codes": ["600519.SH"]}),
    ]
    for res in write_attempts:
        assert res.status_code == 403, f"{res.request.url} 应为只读：{res.status_code} {res.text}"
        assert res.headers.get("X-Auth-Error") == "read_only"

    # 预览不落库，属于只读操作：viewer 也可以用它检查规则会命中什么
    preview = client.post(
        "/api/v1/alerts/preview",
        headers=h,
        json={"name": "x", "rule_type": "price_cross", "params": {"threshold": 1}, "scope_type": "codes"},
    )
    assert preview.status_code == 200

    # 预览不落库，属于只读操作：viewer 也可以用它检查规则会命中什么
    preview = client.post(
        "/api/v1/alerts/preview",
        headers=h,
        json={"name": "x", "rule_type": "price_cross", "params": {"threshold": 1}, "scope_type": "codes"},
    )
    assert preview.status_code == 200


# --------------------------------------------------------------------------- #
# editor：能写数据，不能管成员
# --------------------------------------------------------------------------- #


def test_editor_writes_data_but_cannot_manage_members(ws_client):
    client = ws_client
    ws_id = _create_workspace(client, client.alice)
    assert _add(client, client.alice, ws_id, "bob", "editor")["status"] == 201

    h = _h(client.bob, ws_id)
    added = client.post("/api/v1/watchlist", headers=h, json={"code": "600519.SH", "note": "bob 加的"})
    assert added.status_code == 201, added.text
    assert client.post(
        "/api/v1/alerts/rules",
        headers=h,
        json={"name": "r", "rule_type": "price_cross", "params": {"threshold": 1}, "scope_type": "codes"},
    ).status_code in (200, 201)

    # 成员管理与工作区设置一律 403
    assert _add(client, client.bob, ws_id, "carol", "viewer")["status"] == 403
    assert client.patch(f"/api/v1/workspaces/{ws_id}", headers=h, json={"name": "改名"}).status_code == 403
    assert client.delete(f"/api/v1/workspaces/{ws_id}", headers=h).status_code == 403
    assert (
        client.post(f"/api/v1/workspaces/{ws_id}/transfer", headers=h, json={"user_id": client.bob["id"]}).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/workspaces/{ws_id}/members/{client.alice['id']}", headers=h, json={"role": "viewer"}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/workspaces/{ws_id}/members/{client.alice['id']}", headers=h).status_code == 403


# --------------------------------------------------------------------------- #
# owner：成员管理
# --------------------------------------------------------------------------- #


def test_owner_manages_members(ws_client):
    client = ws_client
    ws_id = _create_workspace(client, client.alice)

    assert _add(client, client.alice, ws_id, "bob", "editor")["status"] == 201
    assert _add(client, client.alice, ws_id, "carol", "viewer")["status"] == 201

    # 重复添加、未知用户、直接授予 owner 都应被拒
    assert _add(client, client.alice, ws_id, "bob", "viewer")["status"] == 409
    assert _add(client, client.alice, ws_id, "nobody", "viewer")["status"] == 404
    assert _add(client, client.alice, ws_id, "carol", "owner")["status"] == 422

    assert _member(client, client.alice, ws_id, client.bob["id"])["role"] == "editor"

    # 改角色
    res = client.patch(
        f"/api/v1/workspaces/{ws_id}/members/{client.bob['id']}",
        headers=_h(client.alice),
        json={"role": "viewer"},
    )
    assert res.status_code == 200 and res.json()["role"] == "viewer"
    # 降级后 bob 立刻变成只读（权限判断不缓存）
    assert (
        client.post("/api/v1/watchlist", headers=_h(client.bob, ws_id), json={"code": "600519.SH"}).status_code == 403
    )

    # 候选列表排除已有成员
    candidates = client.get(f"/api/v1/workspaces/{ws_id}/members/candidates", headers=_h(client.alice)).json()
    names = {row["username"] for row in candidates}
    assert "carol" not in names and "dave" in names

    # 移除成员
    assert (
        client.delete(f"/api/v1/workspaces/{ws_id}/members/{client.carol['id']}", headers=_h(client.alice)).status_code
        == 204
    )
    assert client.get("/api/v1/watchlist", headers=_h(client.carol, ws_id)).status_code == 404


def test_last_owner_can_neither_be_demoted_nor_removed(ws_client):
    client = ws_client
    ws_id = _create_workspace(client, client.alice)
    alice_id = client.alice["id"]

    demote = client.patch(
        f"/api/v1/workspaces/{ws_id}/members/{alice_id}",
        headers=_h(client.alice),
        json={"role": "editor"},
    )
    assert demote.status_code == 400
    assert "所有者" in demote.json()["detail"]

    removed = client.delete(f"/api/v1/workspaces/{ws_id}/members/{alice_id}", headers=_h(client.alice))
    assert removed.status_code == 400


def test_transfer_owner_demotes_previous_owner(ws_client):
    client = ws_client
    ws_id = _create_workspace(client, client.alice)
    assert _add(client, client.alice, ws_id, "bob", "editor")["status"] == 201

    res = client.post(
        f"/api/v1/workspaces/{ws_id}/transfer",
        headers=_h(client.alice),
        json={"user_id": client.bob["id"]},
    )
    assert res.status_code == 200 and res.json()["role"] == "owner"

    # 原 owner 降为 editor：还能写数据，但不能再管成员
    assert _member(client, client.alice, ws_id, client.alice["id"])["role"] == "editor"
    assert (
        client.post("/api/v1/watchlist", headers=_h(client.alice, ws_id), json={"code": "600519.SH"}).status_code == 201
    )
    assert _add(client, client.alice, ws_id, "carol", "viewer")["status"] == 403

    # 转让之后 bob 是唯一 owner，谁也动不了他
    assert (
        client.delete(f"/api/v1/workspaces/{ws_id}/members/{client.bob['id']}", headers=_h(client.alice)).status_code
        == 403
    )


# --------------------------------------------------------------------------- #
# 非成员 / 越界
# --------------------------------------------------------------------------- #


def test_non_member_sees_404(ws_client):
    client = ws_client
    ws_id = _create_workspace(client, client.alice)

    h = _h(client.dave, ws_id)
    assert client.get(f"/api/v1/workspaces/{ws_id}", headers=_h(client.dave)).status_code == 404
    assert client.get("/api/v1/watchlist", headers=h).status_code == 404
    # 写端点也必须是 404（成员身份都没有，谈不上「只读」）
    assert client.post("/api/v1/watchlist", headers=h, json={"code": "600519.SH"}).status_code == 404
    # 不存在的 id 与无权访问返回同样的 404，避免枚举
    assert client.get("/api/v1/workspaces/99999", headers=_h(client.dave)).status_code == 404


def test_workspace_header_switches_scope(ws_client):
    client = ws_client
    personal = client.get("/api/v1/workspaces", headers=_h(client.bob)).json()
    personal_id = next(row["id"] for row in personal if row["kind"] == "personal")

    client.post("/api/v1/watchlist", headers=_h(client.bob, personal_id), json={"code": "600519.SH"})
    ws_id = _create_workspace(client, client.bob, name="团队清单")

    # 新工作区是空的
    assert client.get("/api/v1/watchlist", headers=_h(client.bob, ws_id)).json() == []
    # 写进新工作区，不影响个人空间
    assert (
        client.post("/api/v1/watchlist", headers=_h(client.bob, ws_id), json={"code": "000001.SZ"}).status_code == 201
    )
    assert [r["code"] for r in client.get("/api/v1/watchlist", headers=_h(client.bob, ws_id)).json()] == ["000001.SZ"]
    assert [r["code"] for r in client.get("/api/v1/watchlist", headers=_h(client.bob, personal_id)).json()] == [
        "600519.SH"
    ]

    # 非法头直接 400：静默降级到个人空间会让人以为数据写丢了
    bad = client.get("/api/v1/watchlist", headers={**_h(client.bob), "X-Workspace-Id": "abc"})
    assert bad.status_code == 400


# --------------------------------------------------------------------------- #
# 工作区生命周期
# --------------------------------------------------------------------------- #


def test_personal_workspace_cannot_be_deleted(ws_client):
    client = ws_client
    personal_id = next(
        row["id"]
        for row in client.get("/api/v1/workspaces", headers=_h(client.bob)).json()
        if row["kind"] == "personal"
    )
    res = client.delete(f"/api/v1/workspaces/{personal_id}", headers=_h(client.bob))
    assert res.status_code == 400
    assert "个人工作区" in res.json()["detail"]


def test_delete_workspace_requires_purge(ws_client):
    client = ws_client
    ws_id = _create_workspace(client, client.alice)
    client.post("/api/v1/watchlist", headers=_h(client.alice, ws_id), json={"code": "600519.SH"})

    blocked = client.delete(f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice))
    assert blocked.status_code == 409
    assert "purge" in blocked.json()["detail"]

    purged = client.delete(f"/api/v1/workspaces/{ws_id}?purge=true", headers=_h(client.alice))
    assert purged.status_code == 200
    assert purged.json()["purged"]["watchlist_items"] == 1

    # 工作区与数据都没了，不留孤儿行
    from stockmaster.db import session_scope
    from stockmaster.db.models import WatchlistItem, Workspace

    with session_scope() as session:
        assert session.get(Workspace, ws_id) is None
        assert session.query(WatchlistItem).filter(WatchlistItem.workspace_id == ws_id).count() == 0


def test_admin_lists_every_workspace(ws_client):
    client = ws_client
    mine = client.get("/api/v1/workspaces", headers=_h(client.alice)).json()
    everything = client.get("/api/v1/workspaces?all=true", headers=_h(client.alice)).json()
    assert len(everything) > len(mine)
    # 管理员看别人工作区时按 owner 处理（救援用），但读数据仍要走成员身份
    bob_personal = next(row for row in everything if row["kind"] == "personal" and row["owner_id"] == client.bob["id"])
    assert bob_personal["role"] == "owner"
