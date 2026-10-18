"""工作区隔离、只读角色与自选配额的回归测试。

新增的 watchlist 清单/批量/导入端点全部是写路径，最容易在「漏加 require_editor」
或「漏加 workspace 过滤」上出问题。这里用 owner/editor/viewer 三个账号从两侧交叉
访问，并单独验证导入配额的原子性（409 时不留下半批数据）。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"


def _register(client: TestClient, username: str, *, admin: bool = False) -> dict:
    response = client.post("/api/v1/auth/register", json={"username": username, "password": STRONG})
    assert response.status_code in (200, 201), response.text
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    me = client.get("/api/v1/auth/me", headers=headers).json()
    if admin:
        assert me["role"] == "admin"
    return {"headers": headers, "id": me["id"], "username": username}


def _h(who: dict, workspace_id: int | None = None) -> dict:
    headers = dict(who["headers"])
    if workspace_id is not None:
        headers["X-Workspace-Id"] = str(workspace_id)
    return headers


def _create_workspace(client: TestClient, who: dict, name: str = "投研小组") -> int:
    response = client.post("/api/v1/workspaces", headers=_h(who), json={"name": name})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _add_member(client: TestClient, who: dict, workspace_id: int, username: str, role: str) -> None:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        headers=_h(who),
        json={"username": username, "role": role},
    )
    assert response.status_code == 201, response.text


def _personal_workspace(client: TestClient, who: dict) -> int:
    rows = client.get("/api/v1/workspaces", headers=_h(who)).json()
    personal = [row for row in rows if row["kind"] == "personal"]
    assert len(personal) == 1, personal
    return int(personal[0]["id"])


def _add_watch(
    client: TestClient,
    who: dict,
    workspace_id: int,
    code: str,
    list_name: str = "default",
    note: str = "",
) -> dict:
    response = client.post(
        "/api/v1/watchlist",
        headers=_h(who, workspace_id),
        json={"code": code, "list_name": list_name, "note": note},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def watch_iso_client(tmp_path, monkeypatch):
    """Gate ON + isolation ON + 每个工作区最多 2 只自选。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"watch-iso-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_DATA_ISOLATION", "true")
    monkeypatch.setenv("SM_WORKSPACE_MAX_WATCHLIST_ITEMS", "2")

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
        alice = _register(client, "alice", admin=True)
        bob = _register(client, "bob")
        carol = _register(client, "carol")
        client.alice = alice  # type: ignore[attr-defined]
        client.bob = bob  # type: ignore[attr-defined]
        client.carol = carol  # type: ignore[attr-defined]

        shared_id = _create_workspace(client, alice)
        _add_member(client, alice, shared_id, "bob", "editor")
        _add_member(client, alice, shared_id, "carol", "viewer")
        client.shared_id = shared_id  # type: ignore[attr-defined]
        yield client

    service.reset_throttle()
    session_mod.reset_engine()
    get_settings.cache_clear()


def test_viewer_can_read_but_not_write(watch_iso_client):
    client = watch_iso_client
    shared = client.shared_id
    item = _add_watch(client, client.alice, shared, "600519.SH")

    viewer = _h(client.carol, shared)
    assert client.get("/api/v1/watchlist/lists", headers=viewer).status_code == 200

    rename = client.patch(
        "/api/v1/watchlist/lists/default",
        headers=viewer,
        json={"new_name": "viewer-rename"},
    )
    assert rename.status_code == 403
    assert rename.headers["X-Auth-Error"] == "read_only"

    delete_list = client.delete("/api/v1/watchlist/lists/default", headers=viewer)
    assert delete_list.status_code == 403
    assert delete_list.headers["X-Auth-Error"] == "read_only"

    batch = client.post(
        "/api/v1/watchlist/batch-delete",
        headers=viewer,
        json={"ids": [item["id"]]},
    )
    assert batch.status_code == 403
    assert batch.headers["X-Auth-Error"] == "read_only"

    imported = client.post(
        "/api/v1/watchlist/import",
        headers=viewer,
        json={"list_name": "viewer-import", "codes": ["000001.SZ"]},
    )
    assert imported.status_code == 403
    assert imported.headers["X-Auth-Error"] == "read_only"


def test_lists_are_isolated_between_workspaces(watch_iso_client):
    client = watch_iso_client
    shared = client.shared_id
    bob_personal = _personal_workspace(client, client.bob)

    _add_watch(client, client.alice, shared, "600519.SH")
    _add_watch(client, client.bob, bob_personal, "000001.SZ")

    alice_lists = client.get("/api/v1/watchlist/lists", headers=_h(client.alice, shared)).json()
    assert [(row["list_name"], row["count"]) for row in alice_lists] == [("default", 1)]

    bob_lists = client.get("/api/v1/watchlist/lists", headers=_h(client.bob, bob_personal)).json()
    assert [(row["list_name"], row["count"]) for row in bob_lists] == [("default", 1)]

    alice_codes = [row["code"] for row in client.get("/api/v1/watchlist", headers=_h(client.alice, shared)).json()]
    bob_codes = [row["code"] for row in client.get("/api/v1/watchlist", headers=_h(client.bob, bob_personal)).json()]
    assert alice_codes == ["600519.SH"]
    assert bob_codes == ["000001.SZ"]


def test_import_quota_is_atomic_and_delete_list_frees_it(watch_iso_client):
    client = watch_iso_client
    shared = client.shared_id
    alice = _h(client.alice, shared)

    _add_watch(client, client.alice, shared, "600519.SH")
    _add_watch(client, client.alice, shared, "000001.SZ")

    blocked = client.post(
        "/api/v1/watchlist/import",
        headers=alice,
        json={
            "list_name": "观察池",
            "codes": ["600519.SH", "000001.SZ", "300750.SZ"],
        },
    )
    assert blocked.status_code == 409
    assert blocked.headers["X-Auth-Error"] == "quota_exceeded"
    # 409 之后不能留下任何新行/新清单（一次配额校验、一次事务）。
    assert client.get("/api/v1/watchlist?list_name=观察池", headers=alice).json() == []
    names = [row["list_name"] for row in client.get("/api/v1/watchlist/lists", headers=alice).json()]
    assert "观察池" not in names

    deleted = client.delete("/api/v1/watchlist/lists/default", headers=alice)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == 2

    imported = client.post(
        "/api/v1/watchlist/import",
        headers=alice,
        json={"list_name": "观察池", "codes": ["600519.SH", "000001.SZ"]},
    )
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["added"] == 2
    assert body["quota"] == {
        "resource": "watchlist_items",
        "limit": 2,
        "used": 2,
        "remaining": 0,
    }


def test_batch_delete_foreign_ids_are_missing(watch_iso_client):
    client = watch_iso_client
    shared = client.shared_id
    bob_personal = _personal_workspace(client, client.bob)

    alice_item = _add_watch(client, client.alice, shared, "600519.SH")
    bob_item = _add_watch(client, client.bob, bob_personal, "000001.SZ")

    response = client.post(
        "/api/v1/watchlist/batch-delete",
        headers=_h(client.alice, shared),
        json={"ids": [bob_item["id"], alice_item["id"], 999999]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": 1, "missing": [bob_item["id"], 999999]}

    remaining = client.get("/api/v1/watchlist", headers=_h(client.bob, bob_personal)).json()
    assert [row["code"] for row in remaining] == ["000001.SZ"]
