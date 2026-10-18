"""API 边界输入测试。

只断言一件事：**任何畸形输入都必须是 4xx，不能是 5xx**。这条性质比逐个校对
状态码更重要——5xx 意味着未捕获异常，会污染日志、触发告警，还可能让前端卡在
半渲染状态。具体错误码由各端点的实现细节决定，不在这里锁死。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

BAD_GETS = [
    "/api/v1/stocks?limit=0",
    "/api/v1/stocks?limit=-1",
    "/api/v1/stocks?limit=100000",
    "/api/v1/stocks?q=" + "x" * 500,
    "/api/v1/stocks/NOT-A-CODE/bars",
    "/api/v1/stocks/600519.SH/bars?adjust=bogus",
    "/api/v1/stocks/600519.SH/bars?limit=-5",
    "/api/v1/stocks/NOT-A-CODE/indicators",
    "/api/v1/calendar?start=2026-01-01&end=2020-01-01",
    "/api/v1/calendar?start=not-a-date&end=2026-01-01",
    "/api/v1/predict?horizon=0",
    "/api/v1/predict?horizon=abc",
    "/api/v1/analysis/factors/NOT-A-CODE",
    "/api/v1/market/quotes?codes=",
    "/api/v1/watchlist?list_name=" + "n" * 300,
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"edge-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "false")

    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    from stockmaster.main import app

    with TestClient(app) as test_client:
        yield test_client

    session_mod.reset_engine()
    get_settings.cache_clear()


@pytest.mark.parametrize("path", BAD_GETS)
def test_malformed_get_never_returns_5xx(client, path):
    response = client.get(path)
    assert response.status_code < 500, f"{path} -> {response.status_code} {response.text[:200]}"


def test_watchlist_rejects_empty_code(client):
    response = client.post("/api/v1/watchlist", json={"code": "   "})
    assert 400 <= response.status_code < 500


def test_watchlist_accepts_long_note_without_truncation_error(client):
    """备注是自由文本：长备注可以存，但不能因为长度抛 5xx。"""
    response = client.post("/api/v1/watchlist", json={"code": "600519.SH", "note": "n" * 5000})
    assert response.status_code < 500


def test_watchlist_batch_delete_requires_ids(client):
    response = client.post("/api/v1/watchlist/batch-delete", json={"ids": []})
    assert 400 <= response.status_code < 500


def test_watchlist_import_rejects_oversized_batch(client):
    response = client.post(
        "/api/v1/watchlist/import",
        json={"list_name": "default", "codes": ["600519.SH"] * 2001},
    )
    assert 400 <= response.status_code < 500


def test_alert_rule_rejects_unknown_type(client):
    response = client.post(
        "/api/v1/alerts/rules",
        json={"name": "x", "rule_type": "no_such_rule", "scope_type": "all"},
    )
    assert 400 <= response.status_code < 500


def test_alert_rule_rejects_empty_name(client):
    response = client.post(
        "/api/v1/alerts/rules",
        json={"name": "", "rule_type": "price_cross", "scope_type": "all"},
    )
    assert response.status_code == 422


def test_backtest_rejects_zero_cash(client):
    response = client.post(
        "/api/v1/backtest/run",
        json={"strategy": "buy_hold", "codes": ["600519.SH"], "initial_cash": 0},
    )
    assert response.status_code == 422


def test_unknown_backtest_run_is_404(client):
    assert client.get("/api/v1/backtest/does-not-exist").status_code == 404


def test_system_job_rejects_unknown_job(client):
    response = client.post("/api/v1/system/jobs/not_a_job")
    assert 400 <= response.status_code < 500


def test_restore_inspect_rejects_garbage_upload(client):
    response = client.post(
        "/api/v1/admin/restore/inspect",
        content=b"this is definitely not a zip archive",
        headers={"Content-Type": "application/octet-stream"},
    )
    # 需要管理员权限时是 401/403，否则应是可读的 400
    assert 400 <= response.status_code < 500
