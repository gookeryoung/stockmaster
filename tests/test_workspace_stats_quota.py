"""工作区用量统计端点 + 写路径可选配额。

为什么单开一套 fixture
---------------------
配额是**进程级配置**（``SM_WORKSPACE_MAX_*``），要验证超限行为就必须把 5 个上限都
调到很小（大多是 1）。这与 ``test_workspace.py`` 的「默认不限」前提相反，所以照抄
其账号/工作区 fixture，但额外用 ``monkeypatch.setenv`` 覆盖配额并清掉 settings 缓存。

为什么默认配额必须不影响既有测试
-------------------------------
配额是运维显式开启的治理手段，默认 ``0`` = 不限。本文件既验证「调小后确实拦住」，
也验证「默认全 0 / 未归属桶（``workspace_id<=0``）不拦」，否则升级后单机模式会突然
被工作区额度卡住。

回测端点不去真的跑引擎：配额检查在函数第一行，直接造一条历史 ``backtest_runs``
再 POST 一次就能验证 409 且不消耗 CPU。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"

#: 本文件统一使用的配额环境变量（全部调小，便于快速撞上限）。
QUOTA_ENV = {
    "SM_WORKSPACE_MAX_MEMBERS": "2",
    "SM_WORKSPACE_MAX_WATCHLIST_ITEMS": "1",
    "SM_WORKSPACE_MAX_ALERT_RULES": "1",
    "SM_WORKSPACE_MAX_BACKTEST_RUNS": "1",
    "SM_WORKSPACE_MAX_CHANNELS": "1",
}

#: 与上面 env 一一对应的 ``limits()`` 结果。
EXPECTED_LIMITS = {
    "members": 2,
    "watchlist_items": 1,
    "alert_rules": 1,
    "backtest_runs": 1,
    "channels": 1,
}

USAGE_KEYS = tuple(EXPECTED_LIMITS)


@pytest.fixture
def quota_client(tmp_path, monkeypatch):
    """四个账号：alice(admin/owner) · bob(editor) · carol(viewer) · dave(局外人)。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"quota-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_DATA_ISOLATION", "true")
    for key, value in QUOTA_ENV.items():
        monkeypatch.setenv(key, value)

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


def _add_member(client: TestClient, who: dict, ws_id: int, username: str, role: str):
    return client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        headers=_h(who, ws_id),
        json={"username": username, "role": role},
    )


def _stats(client: TestClient, who: dict, ws_id: int):
    return client.get(f"/api/v1/workspaces/{ws_id}/stats", headers=_h(who, ws_id))


def _add_watchlist(client: TestClient, who: dict, ws_id: int, code: str, note: str = ""):
    return client.post("/api/v1/watchlist", headers=_h(who, ws_id), json={"code": code, "note": note})


def _create_alert(client: TestClient, who: dict, ws_id: int, name: str, params: dict | None = None):
    return client.post(
        "/api/v1/alerts/rules",
        headers=_h(who, ws_id),
        json={
            "name": name,
            "rule_type": "price_cross",
            "params": {"threshold": 100.0} if params is None else params,
        },
    )


def _create_channel(client: TestClient, who: dict, ws_id: int, name: str = "主群"):
    return client.post(
        f"/api/v1/workspaces/{ws_id}/channels",
        headers=_h(who, ws_id),
        json={"name": name, "kind": "webhook", "url": "https://example.com/hook?token=secret1234"},
    )


def _insert_backtest_run(ws_id: int) -> str:
    """直接落一条历史回测：只为了把配额占满，避免真的跑引擎。"""
    from stockmaster.db import session_scope
    from stockmaster.db.models import BacktestRun

    run_id = str(uuid.uuid4())
    with session_scope() as session:
        session.add(
            BacktestRun(
                id=run_id,
                workspace_id=ws_id,
                name="existing run",
                strategy="buy_hold",
                universe="600519.SH",
                status="done",
            )
        )
    return run_id


# --------------------------------------------------------------------------- #
# 用量统计端点
# --------------------------------------------------------------------------- #


def test_stats_reports_usage_limits_and_labels(quota_client):
    client = quota_client
    ws_id = _create_workspace(client, client.alice)

    res = _stats(client, client.alice, ws_id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["workspace_id"] == ws_id
    # 用量：工作区里还没有业务数据，但 owner 本人就是一条成员记录。
    assert body["usage"] == {key: (1 if key == "members" else 0) for key in USAGE_KEYS}
    assert body["total"] == 1
    # 上限与 env 完全一致（0 表示不限的语义由前端展示，接口原样返回数字）
    assert body["limits"] == EXPECTED_LIMITS
    # 中文标签由后端提供，前端不再维护第二份翻译表
    assert body["labels"]["members"] == "成员"
    assert body["labels"]["watchlist_items"] == "自选股"
    assert body["labels"]["channels"] == "通知通道"
    assert len(body["labels"]) == len(USAGE_KEYS)

    # 成员（viewer）可读
    assert _add_member(client, client.alice, ws_id, "carol", "viewer").status_code == 201
    viewer = _stats(client, client.carol, ws_id)
    assert viewer.status_code == 200
    assert viewer.json()["usage"]["members"] == 2

    # 非成员 / 不存在的工作区一律 404
    assert _stats(client, client.dave, ws_id).status_code == 404
    assert _stats(client, client.alice, 99999).status_code == 404


# --------------------------------------------------------------------------- #
# 各写路径的配额
# --------------------------------------------------------------------------- #


def test_watchlist_quota_and_idempotent_update(quota_client):
    client = quota_client
    ws_id = _create_workspace(client, client.alice)

    first = _add_watchlist(client, client.alice, ws_id, "600519.SH")
    assert first.status_code == 201, first.text
    item_id = first.json()["id"]

    # 超过上限：409 + 专用响应头 + 可读的中文提示
    second = _add_watchlist(client, client.alice, ws_id, "000001.SZ")
    assert second.status_code == 409, second.text
    assert second.headers.get("X-Auth-Error") == "quota_exceeded"
    assert "上限" in second.json()["detail"]

    # 幂等更新同一 (list, code) 不占额度：满额时仍然 201
    updated = _add_watchlist(client, client.alice, ws_id, "600519.SH", note="改了备注")
    assert updated.status_code == 201, updated.text
    assert updated.json()["note"] == "改了备注"

    # 删除后腾出额度，可以继续新增
    assert client.delete(f"/api/v1/watchlist/{item_id}", headers=_h(client.alice, ws_id)).status_code == 204
    third = _add_watchlist(client, client.alice, ws_id, "000001.SZ")
    assert third.status_code == 201, third.text


def test_alert_rule_quota_after_parameter_validation(quota_client):
    client = quota_client
    ws_id = _create_workspace(client, client.alice)

    assert _create_alert(client, client.alice, ws_id, "第一条").status_code == 201

    second = _create_alert(client, client.alice, ws_id, "第二条")
    assert second.status_code == 409
    assert second.headers.get("X-Auth-Error") == "quota_exceeded"
    assert "上限" in second.json()["detail"]

    # 参数非法必须先 422：配额的 409 不能掩盖「这个规则根本建不出来」
    invalid = _create_alert(client, client.alice, ws_id, "参数缺失", params={})
    assert invalid.status_code == 422, invalid.text


def test_channel_quota(quota_client):
    client = quota_client
    ws_id = _create_workspace(client, client.alice)

    assert _create_channel(client, client.alice, ws_id, "主群").status_code == 201
    second = _create_channel(client, client.alice, ws_id, "备用群")
    assert second.status_code == 409
    assert second.headers.get("X-Auth-Error") == "quota_exceeded"
    assert "上限" in second.json()["detail"]


def test_member_quota(quota_client):
    client = quota_client
    ws_id = _create_workspace(client, client.alice)

    # owner 已占 1，上限 2：还能加 1 人，第 3 人 409
    assert _add_member(client, client.alice, ws_id, "bob", "editor").status_code == 201
    third = _add_member(client, client.alice, ws_id, "carol", "viewer")
    assert third.status_code == 409
    assert third.headers.get("X-Auth-Error") == "quota_exceeded"
    assert "成员" in third.json()["detail"]
    assert "上限" in third.json()["detail"]


def test_usage_counts_and_backtest_quota(quota_client):
    client = quota_client
    ws_id = _create_workspace(client, client.alice)

    assert _add_member(client, client.alice, ws_id, "bob", "editor").status_code == 201
    assert _add_watchlist(client, client.alice, ws_id, "600519.SH").status_code == 201
    assert _create_alert(client, client.alice, ws_id, "预警").status_code == 201
    assert _create_channel(client, client.alice, ws_id).status_code == 201
    _insert_backtest_run(ws_id)

    expected_usage = {
        "members": 2,
        "watchlist_items": 1,
        "alert_rules": 1,
        "backtest_runs": 1,
        "channels": 1,
    }
    body = _stats(client, client.alice, ws_id).json()
    assert body["usage"] == expected_usage
    assert body["total"] == sum(expected_usage.values())

    # 回测配额在函数第一行生效：已有 1 条记录时直接 409，不会先跑引擎再报错
    rejected = client.post(
        "/api/v1/backtest/run",
        headers=_h(client.alice, ws_id),
        json={"strategy": "buy_hold", "codes": ["600519.SH"]},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.headers.get("X-Auth-Error") == "quota_exceeded"
    assert "上限" in rejected.json()["detail"]
    # 被拒绝的请求没有写入新记录
    assert _stats(client, client.alice, ws_id).json()["usage"]["backtest_runs"] == 1


# --------------------------------------------------------------------------- #
# 默认不限 / 未归属桶放行
# --------------------------------------------------------------------------- #


def test_default_limits_are_all_zero(quota_client, monkeypatch):
    """不配置任何 SM_WORKSPACE_MAX_* 时，所有上限都是 0（不限）。"""
    for key in QUOTA_ENV:
        monkeypatch.delenv(key, raising=False)

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.core import quota

    assert quota.limits() == dict.fromkeys(USAGE_KEYS, 0)

    # limit<=0 时 enforce 直接放行，甚至不会去数一个不存在的工作区
    from stockmaster.db import session_scope

    with session_scope() as session:
        quota.enforce(session, 987654, "watchlist_items")


def test_enforce_allows_unassigned_bucket(quota_client):
    """隔离关闭时所有数据都在 workspace_id=0，配额不生效（升级后行为不变）。"""
    from stockmaster.core import quota
    from stockmaster.db import session_scope

    with session_scope() as session:
        # 此时 fixture 的上限是 1/2（不是 0），仍应放行：workspace_id<=0 优先。
        quota.enforce(session, 0, "watchlist_items")
        quota.enforce(session, 0, "members")
        quota.enforce(session, -1, "channels")
