"""工作区用量趋势快照与 CSV 导出测试。

为什么值得单开一套
------------------
趋势数据的价值全在「口径一致 + 幂等」：快照的 ``data_total`` 必须和
``GET /workspaces/{id}/stats`` 用同一套口径算出来，同一天重复跑任务只能有一行，
否则图上会出现重复点、导出与页面还会对不上账。这里直接跑真实的调度任务函数，
再从数据库读快照来验证这三点。

导出的 CSV 是给 Excel 打开的中文文件，因此额外锁定 BOM 与附件文件名 —— 这两处
出问题（乱码/浏览器把文件当页面打开）不会报错，只会让用户拿到坏文件。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

STRONG = "Passw0rd123"
DATA_COUNTER_KEYS = (
    "watchlist_items",
    "alert_rules",
    "backtest_runs",
)


@pytest.fixture
def usage_client(tmp_path, monkeypatch):
    """三个账号：alice(admin/owner) · bob(成员) · dave(局外人)。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"usage-{uuid.uuid4().hex}.db")
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


def _add_member(client: TestClient, who: dict, ws_id: int, username: str, role: str = "viewer"):
    return client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        headers=_h(who, ws_id),
        json={"username": username, "role": role},
    )


def _add_watchlist(client: TestClient, who: dict, ws_id: int, code: str):
    return client.post("/api/v1/watchlist", headers=_h(who, ws_id), json={"code": code})


def _history(client: TestClient, who: dict, ws_id: int, days: int | None = None):
    suffix = "" if days is None else f"?days={days}"
    return client.get(f"/api/v1/workspaces/{ws_id}/stats/history{suffix}", headers=_h(who, ws_id))


def _export(client: TestClient, who: dict, ws_id: int):
    return client.get(f"/api/v1/workspaces/{ws_id}/stats/export", headers=_h(who, ws_id))


def _stats(client: TestClient, who: dict, ws_id: int) -> dict:
    return client.get(f"/api/v1/workspaces/{ws_id}/stats", headers=_h(who, ws_id)).json()


def _data_total(stats: dict) -> int:
    return sum(stats["usage"][key] for key in DATA_COUNTER_KEYS)


# --------------------------------------------------------------------------- #
# CSV 导出
# --------------------------------------------------------------------------- #


def test_export_csv_is_member_readable_with_bom_and_limits(usage_client):
    client = usage_client
    ws_id = _create_workspace(client, client.alice, name="导出组")
    assert _add_member(client, client.alice, ws_id, "bob").status_code == 201
    assert _add_watchlist(client, client.alice, ws_id, "600519.SH").status_code == 201

    res = _export(client, client.bob, ws_id)

    assert res.status_code == 200, res.text
    assert "text/csv" in res.headers["content-type"]
    # Excel 靠 BOM 识别 UTF-8，否则中文表头会乱码。
    assert res.content.startswith(b"\xef\xbb\xbf")
    text = res.content.decode("utf-8-sig")
    # 元信息 + 表头 + 5 类资源 + 合计。
    assert "导出组" in text
    assert f"工作区 ID,{ws_id}" in text
    assert "资源,名称,用量,上限" in text
    assert "watchlist_items,自选股,1,不限" in text
    assert "members,成员,2,不限" in text
    assert "channels,通知通道,0,不限" in text
    # 合计行 = 全部 5 类资源之和（成员 2 + 自选 1），不是「不限」。
    assert "合计,,3," in text
    # 文件名带工作区 id 与导出日期，下载多份时能区分来源。
    disposition = res.headers["content-disposition"]
    assert "attachment" in disposition
    assert f"workspace-{ws_id}-usage-" in disposition
    assert disposition.endswith('.csv"')

    # 非成员一律 404（不泄露工作区是否存在）。
    assert _export(client, client.dave, ws_id).status_code == 404


# --------------------------------------------------------------------------- #
# 趋势：无快照 / 跑任务 / 幂等 / 多日窗口
# --------------------------------------------------------------------------- #


def test_history_empty_then_job_snapshot_matches_stats(usage_client):
    client = usage_client
    ws_id = _create_workspace(client, client.alice, name="趋势组")
    assert _add_member(client, client.alice, ws_id, "bob", "editor").status_code == 201
    assert _add_watchlist(client, client.alice, ws_id, "600519.SH").status_code == 201

    assert _history(client, client.bob, ws_id).json() == []

    from stockmaster.jobs.tasks import job_snapshot_workspace_usage

    report = job_snapshot_workspace_usage()
    assert report["job"] == "workspace_usage"
    assert report["workspaces"] >= 1
    assert report["captured_on"] == date.today().isoformat()

    rows = _history(client, client.bob, ws_id).json()
    assert len(rows) == 1
    point = rows[0]
    stats = _stats(client, client.alice, ws_id)
    assert point["captured_on"] == date.today().isoformat()
    assert point["member_count"] == stats["usage"]["members"] == 2
    assert point["watchlist_items"] == stats["usage"]["watchlist_items"] == 1
    assert point["data_total"] == _data_total(stats) == 1

    # 同一天再跑一次：只有更新，没有第二个点。
    assert _add_watchlist(client, client.alice, ws_id, "000001.SZ").status_code == 201
    job_snapshot_workspace_usage()
    rows = _history(client, client.bob, ws_id).json()
    assert len(rows) == 1
    assert rows[0]["watchlist_items"] == 2
    assert rows[0]["data_total"] == 2


def test_history_days_window_and_ascending_order(usage_client):
    client = usage_client
    ws_id = _create_workspace(client, client.alice)
    today = date.today()

    from stockmaster.db import session_scope
    from stockmaster.plugins.workspaces.services.usage_history import capture_snapshot

    for offset in (10, 2, 1, 0):
        with session_scope() as session:
            capture_snapshot(session, ws_id, on=today - timedelta(days=offset))

    # 窗口以「最新一条快照」为基准：days=3 → 今天及前两天，升序。
    rows = _history(client, client.alice, ws_id, days=3).json()
    assert [row["captured_on"] for row in rows] == [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    # days=1 只保留最新一天。
    assert [row["captured_on"] for row in _history(client, client.alice, ws_id, days=1).json()] == [today.isoformat()]

    # 参数边界由 FastAPI 校验。
    assert _history(client, client.alice, ws_id, days=0).status_code == 422
    assert _history(client, client.alice, ws_id, days=366).status_code == 422

    # 非成员不可读。
    assert _history(client, client.dave, ws_id).status_code == 404


# --------------------------------------------------------------------------- #
# 任务：多工作区、删除级联、手动触发入口
# --------------------------------------------------------------------------- #


def test_job_snapshots_every_workspace_and_delete_cascades(usage_client):
    client = usage_client
    ws_a = _create_workspace(client, client.alice, name="A 组")
    ws_b = _create_workspace(client, client.alice, name="B 组")

    from stockmaster.db import session_scope
    from stockmaster.db.models import Workspace, WorkspaceUsageSnapshot
    from stockmaster.jobs.tasks import job_snapshot_workspace_usage

    report = job_snapshot_workspace_usage()
    assert report["workspaces"] >= 2

    with session_scope() as session:
        real_ids = set(session.execute(select(Workspace.id)).scalars())
        snapshot_ids = set(session.execute(select(WorkspaceUsageSnapshot.workspace_id).distinct()).scalars())
    # 每个真实工作区（含个人工作区）都有一行；id=0 的未归属桶不存在。
    assert real_ids == snapshot_ids
    assert snapshot_ids >= {ws_a, ws_b}

    # 空工作区可直接删除；快照靠外键级联清理，不留孤儿行。
    assert client.delete(f"/api/v1/workspaces/{ws_b}", headers=_h(client.alice)).status_code == 200
    with session_scope() as session:
        remaining = session.execute(
            select(WorkspaceUsageSnapshot).where(WorkspaceUsageSnapshot.workspace_id == ws_b)
        ).first()
    assert remaining is None


def test_system_job_endpoint_runs_workspace_usage(usage_client):
    client = usage_client

    res = client.post("/api/v1/system/jobs/workspace_usage", headers=_h(client.alice))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["job"] == "workspace_usage"
    assert body["detail"]["job"] == "workspace_usage"
