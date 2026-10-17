"""工作区级预警参数覆盖 API：默认值、owner 写权限、重置与随工作区清理。

为什么写权限只给 owner
--------------------
策略是团队级安全阀（「本工作区所有提醒都排除 ST」这类），editor 能把规则写得更严，
但改策略等于改**所有人**的提醒条件。所以读写分层与通知通道一致：成员可读、仅 owner
可写。

为什么重置用「全 null」表达
-------------------------
PUT 是整体替换：``{}`` / 四个字段全 null → 删除策略行、恢复默认。部分字段为 null 则
表示该维度「跟随规则」，而不是「保持上一次的值」——规则与策略的合并语义在
``plugins/alerts/services/policy.py``，接口不做第二套解释。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"
POLICY_FIELDS = ("exclude_st", "min_amount", "cooldown_seconds", "once_per_day")
DEFAULT_EFFECTIVE = {"exclude_st": False, "min_amount": 0.0, "cooldown_seconds": 0, "once_per_day": False}


@pytest.fixture
def policy_client(tmp_path, monkeypatch):
    """四个账号：alice(admin/owner) · bob(editor) · carol(viewer) · dave(局外人)。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"policy-{uuid.uuid4().hex}.db")
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


def _policy_url(ws_id: int) -> str:
    return f"/api/v1/workspaces/{ws_id}/alert-policy"


def _get_policy(client: TestClient, who: dict, ws_id: int, header_ws: int | None = None):
    return client.get(_policy_url(ws_id), headers=_h(who, header_ws))


def _put_policy(client: TestClient, who: dict, ws_id: int, payload: dict, header_ws: int | None = None):
    return client.put(_policy_url(ws_id), headers=_h(who, header_ws), json=payload)


def _policy_row(ws_id: int):
    from stockmaster.db import session_scope
    from stockmaster.db.models import WorkspaceAlertPolicy

    with session_scope() as session:
        return session.get(WorkspaceAlertPolicy, ws_id)


# --------------------------------------------------------------------------- #
# 默认值 / 读权限
# --------------------------------------------------------------------------- #


def test_default_policy_and_read_access(policy_client):
    client = policy_client
    ws_id = _create_workspace(client, client.alice)

    res = _get_policy(client, client.alice, ws_id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["workspace_id"] == ws_id
    for field in POLICY_FIELDS:
        assert body[field] is None, f"{field} 未配置时应该为 null"
    assert body["updated_at"] is None
    assert body["effective"] == DEFAULT_EFFECTIVE

    # editor / viewer 都能读（但看不到任何「谁改的」内部字段之外的东西）
    _add_member(client, client.alice, ws_id, "bob", "editor")
    _add_member(client, client.alice, ws_id, "carol", "viewer")
    for who in (client.bob, client.carol):
        member_view = _get_policy(client, who, ws_id)
        assert member_view.status_code == 200
        assert member_view.json()["effective"] == DEFAULT_EFFECTIVE

    # 非成员 / 不存在的工作区一律 404（不泄露存在性）
    assert _get_policy(client, client.dave, ws_id).status_code == 404
    assert _get_policy(client, client.alice, 99999).status_code == 404


# --------------------------------------------------------------------------- #
# owner 设置 / 修改 / 重置
# --------------------------------------------------------------------------- #


def test_owner_sets_modifies_and_resets_policy(policy_client):
    client = policy_client
    ws_id = _create_workspace(client, client.alice)

    payload = {
        "exclude_st": True,
        "min_amount": 1_000_000.0,
        "cooldown_seconds": 3600,
        "once_per_day": True,
    }
    res = _put_policy(client, client.alice, ws_id, payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert {field: body[field] for field in POLICY_FIELDS} == payload
    assert body["effective"] == payload
    assert body["updated_at"] is not None

    # GET 持久化；库里记录了操作者
    assert {field: _get_policy(client, client.alice, ws_id).json()[field] for field in POLICY_FIELDS} == payload
    row = _policy_row(ws_id)
    assert row is not None and row.updated_by == client.alice["id"]

    # PUT 是整体替换：只给 min_amount，其余维度被清成 null
    partial = _put_policy(client, client.alice, ws_id, {"min_amount": 2_000_000.0})
    assert partial.status_code == 200, partial.text
    body = partial.json()
    assert body["min_amount"] == 2_000_000.0
    for field in ("exclude_st", "cooldown_seconds", "once_per_day"):
        assert body[field] is None
    assert body["effective"] == {
        "exclude_st": False,
        "min_amount": 2_000_000.0,
        "cooldown_seconds": 0,
        "once_per_day": False,
    }

    # 全 null → 删除策略行，恢复默认
    reset = _put_policy(client, client.alice, ws_id, {})
    assert reset.status_code == 200, reset.text
    reset_body = reset.json()
    for field in POLICY_FIELDS:
        assert reset_body[field] is None
    assert reset_body["updated_at"] is None
    assert reset_body["effective"] == DEFAULT_EFFECTIVE
    assert _policy_row(ws_id) is None

    # 显式传 null 与省略等价，同样是重置
    assert _put_policy(client, client.alice, ws_id, {"exclude_st": None, "min_amount": None}).status_code == 200
    assert _policy_row(ws_id) is None


# --------------------------------------------------------------------------- #
# 写权限与校验
# --------------------------------------------------------------------------- #


def test_only_owner_can_write_policy(policy_client):
    client = policy_client
    ws_id = _create_workspace(client, client.alice)
    _add_member(client, client.alice, ws_id, "bob", "editor")
    _add_member(client, client.alice, ws_id, "carol", "viewer")

    for who in (client.bob, client.carol):
        res = _put_policy(client, who, ws_id, {"exclude_st": True})
        assert res.status_code == 403, f"{who['username']} 不应能改策略：{res.text}"
        assert res.headers.get("X-Auth-Error") == "forbidden"

    # 非成员连写路径都看不到（404 而非 403）
    assert _put_policy(client, client.dave, ws_id, {"exclude_st": True}).status_code == 404
    # 越权尝试没有写库
    assert _policy_row(ws_id) is None


def test_policy_validation(policy_client):
    client = policy_client
    ws_id = _create_workspace(client, client.alice)
    invalid_payloads = [
        {"min_amount": -1},
        {"cooldown_seconds": -1},
        {"cooldown_seconds": 30 * 24 * 3600 + 1},
        {"min_amount": 1_000_000_001},
        {"exclude_st": "maybe"},
    ]
    for payload in invalid_payloads:
        res = _put_policy(client, client.alice, ws_id, payload)
        assert res.status_code == 422, f"{payload} 应被拒：{res.status_code} {res.text}"

    # 非法请求不落库
    assert _policy_row(ws_id) is None


# --------------------------------------------------------------------------- #
# 路径 id 与请求头不一致
# --------------------------------------------------------------------------- #


def test_path_workspace_wins_over_header(policy_client):
    client = policy_client
    ws_a = _create_workspace(client, client.alice, "A 组")
    ws_b = _create_workspace(client, client.alice, "B 组")
    _add_member(client, client.alice, ws_a, "bob", "editor")

    # path_workspace 以路径 id 为准：bob 是 A 的成员，即使头指向 B 也能读 A
    assert _get_policy(client, client.bob, ws_a, header_ws=ws_b).status_code == 200

    # 反过来：bob 不是 B 的成员，header 指向 A 也不能访问 B
    assert _get_policy(client, client.bob, ws_b, header_ws=ws_a).status_code == 404
    assert _put_policy(client, client.bob, ws_b, {"exclude_st": True}, header_ws=ws_a).status_code == 404

    # 写权限同样只认路径：bob 是自己工作区的 owner，即使头指向 A（他仅是 editor）也 200
    ws_own = _create_workspace(client, client.bob, "bob 的空间")
    res = _put_policy(client, client.bob, ws_own, {"exclude_st": True}, header_ws=ws_a)
    assert res.status_code == 200, res.text
    # 写到了路径工作区，而不是头指向的那个
    assert _policy_row(ws_own) is not None
    assert _policy_row(ws_a) is None


# --------------------------------------------------------------------------- #
# 删除工作区时清理策略行
# --------------------------------------------------------------------------- #


def test_deleting_workspace_removes_policy_row(policy_client):
    client = policy_client
    ws_id = _create_workspace(client, client.alice)
    assert _put_policy(client, client.alice, ws_id, {"exclude_st": True, "min_amount": 500.0}).status_code == 200
    assert _policy_row(ws_id) is not None

    # 策略不是业务数据：不应触发「工作区非空，需要 purge」
    res = client.delete(f"/api/v1/workspaces/{ws_id}", headers=_h(client.alice))
    assert res.status_code == 200, res.text
    purged = res.json()["purged"]
    assert all(value == 0 for value in purged.values()), purged
    assert _policy_row(ws_id) is None

    from stockmaster.db import session_scope
    from stockmaster.db.models import Workspace

    with session_scope() as session:
        assert session.get(Workspace, ws_id) is None


# --------------------------------------------------------------------------- #
# effective_policy() 归一化
# --------------------------------------------------------------------------- #


def test_effective_policy_normalizes_values():
    from types import SimpleNamespace

    from stockmaster.plugins.alerts.services.policy import (
        DEFAULT_POLICY,
        EffectivePolicy,
        effective_policy,
    )

    assert effective_policy(None) == DEFAULT_POLICY
    assert effective_policy(None) == EffectivePolicy(
        exclude_st=False, min_amount=0.0, cooldown_seconds=0, once_per_day=False
    )

    # DB 被手工改成负数（或历史数据）时归一为 0，而不是把负数带进扫描逻辑
    defensive = SimpleNamespace(exclude_st=None, min_amount=-5.0, cooldown_seconds=-10, once_per_day=None)
    assert effective_policy(defensive) == EffectivePolicy(
        exclude_st=False, min_amount=0.0, cooldown_seconds=0, once_per_day=False
    )

    # 全字段已设置时取原值（只收紧的合并在引擎侧完成）
    full = SimpleNamespace(exclude_st=True, min_amount=123.0, cooldown_seconds=60, once_per_day=True)
    assert effective_policy(full) == EffectivePolicy(
        exclude_st=True, min_amount=123.0, cooldown_seconds=60, once_per_day=True
    )
