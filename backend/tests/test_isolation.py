"""用户数据隔离测试：自选股 / 预警 / 回测按 ``owner_id`` 互不可见。

为什么要单独一套矩阵
--------------------
隔离的失效方式几乎都不是「条件写错」，而是**漏写**：新增一个端点忘了带
``current_owner``，或者某处查重没加 owner 条件导致两个人互相改写 ``note``。这类
bug 在单用户环境下永远不出现，上线第二天才被发现。所以这里用两个账号从两侧交叉
访问，断言「别人的东西 = 404 或空列表」。

同时保留一组「开关关闭」的用例：默认配置下行为必须与隔离前完全一致，否则升级会
改变现有单机用户的可见数据。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def iso_client(tmp_path, monkeypatch):
    """Gate ON + isolation ON, two accounts (alice = admin, bob = user)."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"iso-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_DATA_ISOLATION", "true")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.auth import service, tokens
    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    tokens.reset_secret_cache()
    service.reset_throttle()

    from app.main import app

    with TestClient(app) as client:
        alice = _register(client, "alice", admin=True)
        bob = _register(client, "bob")
        client.alice = alice  # type: ignore[attr-defined]
        client.bob = bob  # type: ignore[attr-defined]
        yield client

    service.reset_throttle()
    session_mod.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def shared_client(tmp_path, monkeypatch):
    """Gate OFF + isolation OFF: the historical single-machine behaviour."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"shared-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "false")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_DATA_ISOLATION", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    from app.main import app

    with TestClient(app) as client:
        yield client

    session_mod.reset_engine()
    get_settings.cache_clear()


def _register(client: TestClient, username: str, *, admin: bool = False) -> dict:
    """Create an account and return ``{"headers": ..., "id": ..., "username": ...}``."""
    res = client.post("/api/v1/auth/register", json={"username": username, "password": STRONG})
    assert res.status_code in (200, 201), res.text
    body = res.json()
    headers = {"Authorization": f"Bearer {body['token']}"}
    me = client.get("/api/v1/auth/me", headers=headers).json()
    if admin:
        assert me["role"] == "admin", "首个账号应为管理员"
    return {"headers": headers, "id": me["id"], "username": username}


def _auth(who: dict) -> dict:
    return who["headers"]


def _seed_backtest(owner_id: int, run_id: str | None = None) -> str:
    """Insert a finished run directly — the API path runs a real backtest."""
    from app.db import session_scope
    from app.db.models import BacktestRun

    run_id = run_id or str(uuid.uuid4())
    with session_scope() as session:
        session.add(
            BacktestRun(
                id=run_id,
                owner_id=owner_id,
                name="seed",
                strategy="momentum",
                universe="default",
                metrics_json="{}",
                cost_json="{}",
                status="done",
            )
        )
    return run_id


# --------------------------------------------------------------------------- #
# Self-contained endpoints
# --------------------------------------------------------------------------- #


def test_watchlist_is_per_user(iso_client):
    client, alice, bob = iso_client, iso_client.alice, iso_client.bob

    added = client.post(
        "/api/v1/watchlist",
        headers=_auth(alice),
        json={"list_name": "default", "code": "600519.SH", "note": "alice 的茅台"},
    )
    assert added.status_code == 201, added.text
    item_id = added.json()["id"]

    assert [row["code"] for row in client.get("/api/v1/watchlist", headers=_auth(alice)).json()] == ["600519.SH"]
    assert client.get("/api/v1/watchlist", headers=_auth(bob)).json() == []

    # 别人的条目对 bob 不存在，连删除都应该是 404 而不是 403（403 会泄露 id 存在）
    assert client.delete(f"/api/v1/watchlist/{item_id}", headers=_auth(bob)).status_code == 404
    assert client.delete(f"/api/v1/watchlist/{item_id}", headers=_auth(alice)).status_code == 204


def test_same_symbol_in_same_list_name_does_not_collide(iso_client):
    """旧唯一约束 ``(list_name, code)`` 会让第二个人的 note 覆盖第一个人的。"""
    client, alice, bob = iso_client, iso_client.alice, iso_client.bob

    first = client.post(
        "/api/v1/watchlist",
        headers=_auth(alice),
        json={"list_name": "default", "code": "000001.SZ", "note": "alice"},
    )
    second = client.post(
        "/api/v1/watchlist",
        headers=_auth(bob),
        json={"list_name": "default", "code": "000001.SZ", "note": "bob"},
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    rows = {row["code"]: row for row in client.get("/api/v1/watchlist", headers=_auth(alice)).json()}
    assert rows["000001.SZ"]["note"] == "alice"

    # 同一个人的重复 POST 仍然是幂等的：更新 note，不新增行
    again = client.post(
        "/api/v1/watchlist",
        headers=_auth(alice),
        json={"list_name": "default", "code": "000001.SZ", "note": "alice-2"},
    )
    assert again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert len(client.get("/api/v1/watchlist", headers=_auth(alice)).json()) == 1


def test_alert_rules_are_per_user(iso_client):
    client, alice, bob = iso_client, iso_client.alice, iso_client.bob

    created = client.post(
        "/api/v1/alerts/rules",
        headers=_auth(alice),
        json={
            "name": "突破 60 日高",
            "rule_type": "new_high",
            "params": {"window": 60},
            "scope_type": "codes",
            "scope_value": "600519.SH",
        },
    )
    assert created.status_code in (200, 201), created.text
    rule_id = created.json()["id"]

    assert len(client.get("/api/v1/alerts/rules", headers=_auth(alice)).json()) == 1
    assert client.get("/api/v1/alerts/rules", headers=_auth(bob)).json() == []

    # bob 不能改、不能删。PATCH 是「整体替换」，必须给完整 payload，否则会先被
    # 请求体校验拦成 422，测不到归属判断。
    hijack = {
        "name": "被 bob 改名",
        "rule_type": "new_high",
        "params": {"window": 60},
        "scope_type": "codes",
        "scope_value": "600519.SH",
    }
    assert client.patch(f"/api/v1/alerts/rules/{rule_id}", headers=_auth(bob), json=hijack).status_code == 404
    assert client.delete(f"/api/v1/alerts/rules/{rule_id}", headers=_auth(bob)).status_code == 404


def test_backtest_history_is_per_user(iso_client):
    client, alice, bob = iso_client, iso_client.alice, iso_client.bob

    alice_run = _seed_backtest(alice["id"])
    bob_run = _seed_backtest(bob["id"])

    mine = [row["run_id"] for row in client.get("/api/v1/backtest", headers=_auth(alice)).json()]
    assert mine == [alice_run]
    assert client.get(f"/api/v1/backtest/{bob_run}", headers=_auth(alice)).status_code == 404
    assert client.get(f"/api/v1/backtest/{alice_run}", headers=_auth(alice)).status_code == 200


def test_trigger_history_is_per_user(iso_client):
    """触发记录冗余了 owner，历史页不需要 join 回规则表。"""
    client, alice, bob = iso_client, iso_client.alice, iso_client.bob

    from app.db import session_scope
    from app.db.models import AlertRule, AlertTrigger

    with session_scope() as session:
        rule = AlertRule(owner_id=alice["id"], name="r", rule_type="price_cross", params_json="{}")
        session.add(rule)
        session.flush()
        session.add(
            AlertTrigger(
                owner_id=alice["id"],
                rule_id=rule.id,
                code="600519.SH",
                name="贵州茅台",
                trade_date=__import__("datetime").date(2026, 10, 16),
                rule_type="price_cross",
                message="m",
                dedup_key="k1",
            )
        )

    assert len(client.get("/api/v1/alerts/triggers", headers=_auth(alice)).json()) == 1
    assert client.get("/api/v1/alerts/triggers", headers=_auth(bob)).json() == []


def test_watchlist_rule_scope_only_sees_own_list(iso_client):
    """``scope=watchlist`` 是最容易串台的地方：解析作用域时忘了 owner。"""
    client, alice, bob = iso_client, iso_client.alice, iso_client.bob

    client.post("/api/v1/watchlist", headers=_auth(alice), json={"code": "600519.SH"})
    client.post("/api/v1/watchlist", headers=_auth(bob), json={"code": "000001.SZ"})

    from app.alert.engine import AlertEngine
    from app.db import session_scope
    from app.db.models import AlertRule

    with session_scope() as session:
        rule = AlertRule(
            owner_id=bob["id"],
            name="bob 的自选",
            rule_type="price_cross",
            params_json="{}",
            scope_type="watchlist",
            scope_value="default",
        )
        codes = AlertEngine()._resolve_scope(session, rule)

    assert codes == ["000001.SZ"], "bob 的规则只应看到 bob 的自选"


def test_manual_scan_only_touches_own_rules(iso_client):
    """手动扫描写的是触发记录：扫别人的规则等于往别人历史里塞数据。"""
    client, alice, bob = iso_client, iso_client.alice, iso_client.bob

    client.post(
        "/api/v1/alerts/rules",
        headers=_auth(alice),
        json={"name": "a", "rule_type": "price_cross", "params": {"threshold": 1}, "scope_type": "codes"},
    )

    # bob 没有任何规则 → 扫描结果为空，且不会把 alice 的规则算进去
    scanned = client.post("/api/v1/alerts/scan", headers=_auth(bob))
    assert scanned.status_code == 200
    assert scanned.json()["triggered"] == 0


# --------------------------------------------------------------------------- #
# Switch off: behaviour must be byte-identical to pre-isolation
# --------------------------------------------------------------------------- #


def test_isolation_off_keeps_shared_semantics(shared_client):
    """默认配置下所有人共享一份数据，单机用户升级后看不到任何变化。"""
    client = shared_client

    added = client.post("/api/v1/watchlist", json={"list_name": "default", "code": "600519.SH", "note": "共享"})
    assert added.status_code == 201

    rows = client.get("/api/v1/watchlist").json()
    assert [row["code"] for row in rows] == ["600519.SH"]

    # 落库的 owner_id 是「未归属」，这正是隔离前所有数据的样子
    from app.db import session_scope
    from app.db.models import WatchlistItem

    with session_scope() as session:
        assert session.query(WatchlistItem).one().owner_id == 0


def test_cli_claim_moves_unowned_rows(iso_client):
    """``user claim`` 把 owner=0 的历史数据交给账号，重复条目只删旧副本。"""
    from app.cli import _claim_ownership
    from app.db import session_scope
    from app.db.models import User, WatchlistItem

    # 模拟隔离前的历史数据
    with session_scope() as session:
        session.add(WatchlistItem(owner_id=0, list_name="default", code="600519.SH", note="老数据"))
        session.add(WatchlistItem(owner_id=0, list_name="default", code="000001.SZ", note="老数据2"))

    bob = iso_client.bob
    with session_scope() as session:
        user = session.get(User, bob["id"])
        assert _claim_ownership(session, user, dry_run=False) == 0

    with session_scope() as session:
        rows = session.query(WatchlistItem).filter(WatchlistItem.owner_id == bob["id"]).all()
        assert {row.code for row in rows} == {"600519.SH", "000001.SZ"}
        assert session.query(WatchlistItem).filter(WatchlistItem.owner_id == 0).count() == 0
