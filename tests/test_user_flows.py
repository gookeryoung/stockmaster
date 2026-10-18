"""关键用户流程的端到端测试（HTTP 层）。

与 ``test_api.py`` 的区别：那一组按端点组织，验证「接口对不对」；这里按**用户旅程**
组织，验证「一个人从注册到拿到回测结果的整条路是否通」——中间经过鉴权中间件、
工作区隔离、配额、预警引擎与回测引擎，任何一环的接线断了都会在这里暴露。

刻意保持离线：数据来自 MockProvider + 直接写库的确定性日线。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

PASSWORD = "Passw0rd123"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def business_days(count: int, start: date = date(2025, 1, 6)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def client(tmp_path, monkeypatch):
    """鉴权 + 工作区隔离都打开的完整应用，带两只个股的确定性日线。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"flow-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_AUTH_MAX_FAILED_LOGINS", "0")
    monkeypatch.setenv("SM_DATA_ISOLATION", "true")

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import session as session_mod
    from stockmaster.plugins.auth.services import service as auth_service
    from stockmaster.plugins.auth.services import tokens

    session_mod.reset_engine()
    session_mod.init_db()
    tokens.reset_secret_cache()
    auth_service.reset_throttle()

    from stockmaster.db import session_scope
    from stockmaster.providers.base import Bar, InstrumentInfo
    from stockmaster.sync import repository as repo

    days = business_days(320)
    with session_scope() as session:
        repo.upsert_calendar(session, days, source="test")
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code="600519.SH", name="贵州茅台", market="SH", board="main", status=1),
                InstrumentInfo(code="000001.SZ", name="平安银行", market="SZ", board="main", status=1),
            ],
        )
        for code, base in (("600519.SH", 100.0), ("000001.SZ", 10.0)):
            bars = []
            previous = base
            for index, day in enumerate(days):
                close = base * (1 + 0.001 * ((index % 7) - 3))
                bars.append(
                    Bar(
                        code=code,
                        trade_date=day,
                        open=close * 0.995,
                        high=close * 1.01,
                        low=close * 0.99,
                        close=close,
                        volume=1e6,
                        amount=1e8,
                        preclose=previous,
                        pct_chg=(close / previous - 1) * 100,
                        turn=1.5,
                        is_st=False,
                        adjust="qfq",
                        source="test",
                    )
                )
                previous = close
            repo.upsert_daily_bars(session, code, bars, "qfq")
            repo.upsert_daily_bars(session, code, bars, "raw")

    from stockmaster.main import app

    with TestClient(app) as test_client:
        yield test_client

    auth_service.reset_throttle()
    session_mod.reset_engine()
    get_settings.cache_clear()


def register(client: TestClient, username: str) -> dict:
    response = client.post("/api/v1/auth/register", json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 主旅程
# --------------------------------------------------------------------------- #


def test_full_user_journey(client):
    """注册 → 建工作区 → 加成员 → 自选 → 预警 → 回测 → 登出。"""
    # 1) 首个注册用户自动成为管理员，拿到可直接使用的 token
    owner = register(client, "owner1")
    assert owner["user"]["role"] == "admin"
    owner_headers = bearer(owner["token"])

    me = client.get("/api/v1/auth/me", headers=owner_headers)
    assert me.status_code == 200
    assert me.json()["username"] == "owner1"

    # 2) 个人工作区随账号创建
    workspaces = client.get("/api/v1/workspaces", headers=owner_headers).json()
    assert len(workspaces) == 1
    assert workspaces[0]["role"] == "owner"

    # 3) 建共享工作区并邀请只读成员
    created = client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "研究小组", "description": "e2e"},
    )
    assert created.status_code == 201, created.text
    shared_id = created.json()["id"]

    viewer = register(client, "viewer1")
    viewer_headers = bearer(viewer["token"])

    added = client.post(
        f"/api/v1/workspaces/{shared_id}/members",
        headers=owner_headers,
        json={"username": "viewer1", "role": "viewer"},
    )
    assert added.status_code == 201, added.text
    assert added.json()["role"] == "viewer"

    # 4) 写操作必须带工作区头：owner 在共享工作区加自选成功
    shared_headers = {**owner_headers, "X-Workspace-Id": str(shared_id)}
    watch = client.post("/api/v1/watchlist", headers=shared_headers, json={"code": "600519.SH"})
    assert watch.status_code == 201, watch.text

    owner_list = client.get("/api/v1/watchlist", headers=shared_headers).json()
    assert [item["code"] for item in owner_list] == ["600519.SH"]

    # 5) 隔离：viewer 用同一个工作区头能看到这条数据，但自己的个人工作区是空的
    viewer_shared = {**viewer_headers, "X-Workspace-Id": str(shared_id)}
    assert [item["code"] for item in client.get("/api/v1/watchlist", headers=viewer_shared).json()] == ["600519.SH"]
    assert client.get("/api/v1/watchlist", headers=viewer_headers).json() == []

    # 6) 只读成员不能写：403 + 稳定错误码，前端据此禁用按钮
    forbidden = client.post("/api/v1/watchlist", headers=viewer_shared, json={"code": "000001.SZ"})
    assert forbidden.status_code == 403
    assert forbidden.headers.get("X-Auth-Error") == "read_only"

    # 7) 预警规则：创建 → 预览 → 扫描
    rule_payload = {
        "name": "放量突破",
        "rule_type": "price_cross",
        "params": {"threshold": 1, "direction": "above"},
        "scope_type": "watchlist",
        "scope_value": "default",
    }
    rule = client.post(
        "/api/v1/alerts/rules",
        headers=shared_headers,
        json=rule_payload,
    )
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["id"]

    preview = client.post("/api/v1/alerts/preview", headers=shared_headers, json=rule_payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["scope_size"] >= 1

    scan = client.post("/api/v1/alerts/scan", headers=shared_headers)
    assert scan.status_code == 200, scan.text
    assert scan.json()["triggered"] >= 1

    triggers = client.get("/api/v1/alerts/triggers", headers=shared_headers).json()
    assert triggers and triggers[0]["rule_id"] == rule_id

    # 8) 回测：能跑出结果并落库，再次查询可读回
    run = client.post(
        "/api/v1/backtest/run",
        headers=shared_headers,
        json={
            "strategy": "buy_hold",
            "codes": ["600519.SH"],
            "start": "2025-01-06",
            "end": "2026-02-01",
            "initial_cash": 1_000_000,
        },
    )
    assert run.status_code == 200, run.text
    body = run.json()
    run_id = body["run_id"]
    assert body["metrics"]["n_trades"] >= 1
    assert body["equity"]

    detail = client.get(f"/api/v1/backtest/{run_id}", headers=shared_headers)
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id

    # viewer 不能发起回测（消耗 CPU 且写入共享历史）
    blocked = client.post(
        "/api/v1/backtest/run",
        headers=viewer_shared,
        json={"strategy": "buy_hold", "codes": ["600519.SH"]},
    )
    assert blocked.status_code == 403

    # 9) 登出后 token 立即失效
    assert client.post("/api/v1/auth/logout", headers=owner_headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=owner_headers).status_code == 401


# --------------------------------------------------------------------------- #
# 边界
# --------------------------------------------------------------------------- #


def test_unauthenticated_access_is_rejected(client):
    response = client.get("/api/v1/watchlist")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_invalid_token_is_rejected_without_500(client):
    response = client.get("/api/v1/watchlist", headers=bearer("not-a-jwt"))
    assert response.status_code == 401


def test_duplicate_username_is_conflict(client):
    register(client, "dup1")
    again = client.post(
        "/api/v1/auth/register",
        json={"username": "dup1", "password": PASSWORD},
    )
    assert again.status_code == 409
    # 鉴权错误把稳定错误码放在 X-Auth-Error 头，detail 留给人读
    assert again.headers.get("X-Auth-Error") == "username_taken"


def test_weak_password_is_rejected(client):
    response = client.post("/api/v1/auth/register", json={"username": "weak1", "password": "123"})
    assert response.status_code in (400, 422)


def test_foreign_workspace_is_not_visible(client):
    owner = register(client, "owner2")
    other = register(client, "other2")
    mine = client.get("/api/v1/workspaces", headers=bearer(owner["token"])).json()[0]

    # 别人的工作区不能读，也不能写（404 而不是 403，避免泄露存在性）
    stolen = {**bearer(other["token"]), "X-Workspace-Id": str(mine["id"])}
    assert client.get("/api/v1/watchlist", headers=stolen).status_code == 404
    assert client.post("/api/v1/watchlist", headers=stolen, json={"code": "600519.SH"}).status_code == 404


def test_invalid_workspace_header_is_rejected(client):
    owner = register(client, "owner3")
    headers = {**bearer(owner["token"]), "X-Workspace-Id": "999999"}
    assert client.get("/api/v1/watchlist", headers=headers).status_code == 404

    bad = {**bearer(owner["token"]), "X-Workspace-Id": "abc"}
    assert client.get("/api/v1/watchlist", headers=bad).status_code in (400, 404, 422)


def test_unknown_symbol_is_rejected_on_write(client):
    owner = register(client, "owner4")
    headers = bearer(owner["token"])

    response = client.post("/api/v1/watchlist", headers=headers, json={"code": "999999.XX"})
    assert response.status_code in (400, 404, 422)


def test_backtest_rejects_inverted_date_range(client):
    owner = register(client, "owner5")
    headers = bearer(owner["token"])

    response = client.post(
        "/api/v1/backtest/run",
        headers=headers,
        json={"strategy": "buy_hold", "codes": ["600519.SH"], "start": "2026-01-01", "end": "2025-01-01"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]


def test_backtest_rejects_out_of_range_parameters(client):
    owner = register(client, "owner6")
    headers = bearer(owner["token"])

    response = client.post(
        "/api/v1/backtest/run",
        headers=headers,
        json={"strategy": "buy_hold", "codes": ["600519.SH"], "top_n": 0, "initial_cash": 0},
    )
    assert response.status_code == 422


def test_backtest_without_data_returns_readable_error(client):
    owner = register(client, "owner7")
    headers = bearer(owner["token"])

    response = client.post(
        "/api/v1/backtest/run",
        headers=headers,
        json={"strategy": "buy_hold", "codes": ["000858.SZ"]},
    )
    assert response.status_code in (400, 409)
    assert "bars" in str(response.json()["detail"]).lower()


def test_quota_blocks_second_watchlist_item(client, monkeypatch):
    """配额是工作区级上限：超限返回 409 + quota_exceeded，而不是 500。"""
    from stockmaster.config import get_settings

    owner = register(client, "owner8")
    headers = bearer(owner["token"])
    assert client.post("/api/v1/watchlist", headers=headers, json={"code": "600519.SH"}).status_code == 201

    get_settings.cache_clear()
    monkeypatch.setenv("SM_WORKSPACE_MAX_WATCHLIST_ITEMS", "1")
    get_settings.cache_clear()

    response = client.post("/api/v1/watchlist", headers=headers, json={"code": "000001.SZ"})
    assert response.status_code == 409
    assert response.headers.get("X-Auth-Error") == "quota_exceeded"
