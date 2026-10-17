"""工作区级策略配置：选股 / 样本 / 算法的 CRUD、权限、隔离与求值。

为什么要有这一套
----------------
这三张表把「一次性表单」变成「可复用、可协作的命名配置」，因此它们同时继承了
两类高风险约束：

* **工作区边界**：跨工作区访问必须 404，引用（样本→选股、算法→样本）也不能
  穿透 —— 否则一次 ``screening_id`` 引用就能把别人的配置变成自己的训练输入；
* **角色边界**：viewer 只能读 + 跑只读求值（选股求值、样本解析），所有写操作
  与训练一律 403 且带 ``X-Auth-Error: read_only``。

求值部分单独覆盖，是因为「配置存进去了」不等于「配置按预期工作」：过滤/排序/
截断、四种标的池来源、日期窗口语义、无数据时返回空结果而不是 404，都是前端
依赖的契约。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"
SCREEN_TRADE_DATE = date(2026, 1, 15)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def strategy_client(tmp_path, monkeypatch):
    """隔离开启 + 鉴权开启：alice(admin) / bob / carol / dave 四个账号。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"strategy-{uuid.uuid4().hex}.db")
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
        client.alice = _register(client, "alice", admin=True)  # type: ignore[attr-defined]
        client.bob = _register(client, "bob")  # type: ignore[attr-defined]
        client.carol = _register(client, "carol")  # type: ignore[attr-defined]
        client.dave = _register(client, "dave")  # type: ignore[attr-defined]
        yield client

    service.reset_throttle()
    session_mod.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def shared_client(tmp_path, monkeypatch):
    """隔离关闭 + 鉴权关闭：与引入工作区之前逐字节一致的共享语义。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"strategy-shared-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "false")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")
    monkeypatch.setenv("SM_DATA_ISOLATION", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.auth import service
    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    service.reset_throttle()

    from app.main import app

    with TestClient(app) as client:
        yield client

    service.reset_throttle()
    session_mod.reset_engine()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 账号 / 工作区 / 配置创建辅助
# --------------------------------------------------------------------------- #


def _register(client: TestClient, username: str, *, admin: bool = False) -> dict:
    res = client.post("/api/v1/auth/register", json={"username": username, "password": STRONG})
    assert res.status_code in (200, 201), res.text
    headers = {"Authorization": f"Bearer {res.json()['token']}"}
    me = client.get("/api/v1/auth/me", headers=headers).json()
    if admin:
        assert me["role"] == "admin", "首个注册账号应为管理员"
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


def _add_member(client: TestClient, who: dict, ws_id: int, username: str, role: str) -> int:
    res = client.post(
        f"/api/v1/workspaces/{ws_id}/members",
        headers=_h(who),
        json={"username": username, "role": role},
    )
    return res.status_code


def _create_screening(
    client: TestClient,
    who: dict,
    ws_id: int,
    name: str,
    *,
    params: dict | None = None,
    expect: int = 201,
) -> dict:
    res = client.post(
        "/api/v1/strategy/screenings",
        headers=_h(who, ws_id),
        json={"name": name, "params": params or {}},
    )
    assert res.status_code == expect, f"{res.status_code}: {res.text}"
    return res.json()


def _create_sample(client: TestClient, who: dict, ws_id: int, name: str, *, expect: int = 201, **fields) -> dict:
    payload = {"name": name, **fields}
    res = client.post("/api/v1/strategy/samples", headers=_h(who, ws_id), json=payload)
    assert res.status_code == expect, f"{res.status_code}: {res.text}"
    return res.json()


def _create_algorithm(client: TestClient, who: dict, ws_id: int, name: str, *, expect: int = 201, **fields) -> dict:
    payload = {"name": name, **fields}
    res = client.post("/api/v1/strategy/algorithms", headers=_h(who, ws_id), json=payload)
    assert res.status_code == expect, f"{res.status_code}: {res.text}"
    return res.json()


def _run_screening(client: TestClient, who: dict, ws_id: int, screening_id: int) -> dict:
    res = client.post(f"/api/v1/strategy/screenings/{screening_id}/run", headers=_h(who, ws_id))
    assert res.status_code == 200, f"{res.status_code}: {res.text}"
    return res.json()


def _resolve_sample(client: TestClient, who: dict, ws_id: int, sample_id: int) -> dict:
    res = client.post(f"/api/v1/strategy/samples/{sample_id}/resolve", headers=_h(who, ws_id))
    assert res.status_code == 200, f"{res.status_code}: {res.text}"
    return res.json()


# --------------------------------------------------------------------------- #
# 数据种子
# --------------------------------------------------------------------------- #


def _seed_screening_data(trade_date: date = SCREEN_TRADE_DATE) -> None:
    """一只 ST + 四个正常标的，同一交易日各一行因子；分数/动量互不相同。"""
    from app.db import session_scope
    from app.db.models import Factor
    from app.providers.base import InstrumentInfo
    from app.sync import repository as repo

    instruments = [
        InstrumentInfo(code="600519.SH", name="贵州茅台", market="SH", board="main", status=1),
        InstrumentInfo(code="000001.SZ", name="平安银行", market="SZ", board="main", status=1),
        InstrumentInfo(code="300750.SZ", name="宁德时代", market="SZ", board="chinext", status=1),
        InstrumentInfo(code="688981.SH", name="中芯国际", market="SH", board="star", status=1),
        InstrumentInfo(code="600000.SH", name="浦发银行", market="SH", board="main", is_st=True, status=1),
    ]
    factors = [
        # code, score, mom_20, vol_20, turnover_20, amount_log_20, rsi_14
        ("600519.SH", 80.0, 1.2, 0.20, 1.5, 10.0, 55.0),
        ("000001.SZ", 60.0, 0.6, 0.50, 1.0, 9.0, 45.0),
        ("300750.SZ", 90.0, 0.4, 0.70, 2.5, 9.5, 65.0),
        ("688981.SH", 70.0, -0.1, 0.35, 3.0, 9.2, 50.0),
        ("600000.SH", 95.0, 2.0, 0.30, 1.2, 9.8, 58.0),
    ]
    with session_scope() as session:
        repo.upsert_instruments(session, instruments)
        for code, score, mom_20, vol_20, turnover_20, amount_log_20, rsi_14 in factors:
            session.add(
                Factor(
                    code=code,
                    trade_date=trade_date,
                    score=score,
                    mom_20=mom_20,
                    vol_20=vol_20,
                    turnover_20=turnover_20,
                    amount_log_20=amount_log_20,
                    rsi_14=rsi_14,
                )
            )


def _seed_bars(
    codes: list[str],
    start: date = date(2024, 1, 1),
    end: date = date(2025, 12, 31),
) -> None:
    """确定性合成日线（qfq），供样本解析的 lookback 与算法训练/回测消费。"""
    from app.db import session_scope
    from app.providers.mock_provider import synthetic_bars
    from app.sync import repository as repo

    with session_scope() as session:
        for code in codes:
            bars = synthetic_bars(code, start, end, adjust="qfq")
            repo.upsert_daily_bars(session, code, bars, "qfq")


def _seed_instruments(codes: list[str]) -> None:
    from app.db import session_scope
    from app.providers.base import InstrumentInfo
    from app.sync import repository as repo

    with session_scope() as session:
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code=code, name=f"标的{index}", market=code.split(".")[1], board="main")
                for index, code in enumerate(codes)
            ],
        )


# --------------------------------------------------------------------------- #
# 1. CRUD
# --------------------------------------------------------------------------- #


def test_screening_config_crud(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    created = _create_screening(
        client,
        client.alice,
        ws_id,
        "低估值动量",
        params={"min_score": 70.0, "exclude_st": True, "order_by": "score", "direction": "desc", "limit": 20},
    )
    assert created["id"] > 0
    assert created["workspace_id"] == ws_id
    assert created["name"] == "低估值动量"
    assert created["params"]["min_score"] == 70.0
    assert created["params"]["limit"] == 20
    assert created["created_by"] == client.alice["id"]

    fetched = client.get(f"/api/v1/strategy/screenings/{created['id']}", headers=h)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "低估值动量"

    patched = client.patch(
        f"/api/v1/strategy/screenings/{created['id']}",
        headers=h,
        json={"name": "低估值动量-v2", "description": "改了名字和条件", "params": {"min_score": 50.0}},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "低估值动量-v2"
    assert body["description"] == "改了名字和条件"
    # params 是整体替换而不是深合并：显式重置字段应回到默认值。
    assert body["params"]["min_score"] == 50.0
    assert body["params"]["limit"] == 50

    deleted = client.delete(f"/api/v1/strategy/screenings/{created['id']}", headers=h)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/strategy/screenings/{created['id']}", headers=h).status_code == 404


def test_sample_config_crud(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    created = _create_sample(
        client,
        client.alice,
        ws_id,
        "两年训练样本",
        universe_type="codes",
        universe_value="600519.SH, 000001.SZ",
        start_date="2024-01-01",
        end_date="2025-12-31",
        horizon=3,
        min_bars=120,
    )
    assert created["id"] > 0
    assert created["workspace_id"] == ws_id
    assert created["universe_type"] == "codes"
    # 创建时就把代码规范化并去重，而不是到解析时才暴露脏数据。
    assert created["universe_value"] == "600519.SH,000001.SZ"
    assert created["start_date"] == "2024-01-01"
    assert created["end_date"] == "2025-12-31"
    assert created["horizon"] == 3
    assert created["min_bars"] == 120

    fetched = client.get(f"/api/v1/strategy/samples/{created['id']}", headers=h)
    assert fetched.status_code == 200
    assert fetched.json()["universe_value"] == "600519.SH,000001.SZ"

    patched = client.patch(
        f"/api/v1/strategy/samples/{created['id']}",
        headers=h,
        json={"universe_value": "300750.SZ", "horizon": 5},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["universe_value"] == "300750.SZ"
    assert body["horizon"] == 5
    # 未传的字段保持不变（PATCH 语义）。
    assert body["start_date"] == "2024-01-01"

    assert client.delete(f"/api/v1/strategy/samples/{created['id']}", headers=h).status_code == 204
    assert client.get(f"/api/v1/strategy/samples/{created['id']}", headers=h).status_code == 404


def test_algorithm_config_crud(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    sample = _create_sample(
        client, client.alice, ws_id, "算法绑定样本", universe_type="codes", universe_value="600519.SH"
    )
    created = _create_algorithm(
        client,
        client.alice,
        ws_id,
        "逻辑回归基线",
        algo="logistic",
        params={"C": 0.8, "max_iter": 200},
        sample_id=sample["id"],
        horizon=1,
        test_fraction=0.2,
    )
    assert created["id"] > 0
    assert created["workspace_id"] == ws_id
    assert created["algo"] == "logistic"
    assert created["params"] == {"C": 0.8, "max_iter": 200}
    assert created["sample_id"] == sample["id"]
    assert created["test_fraction"] == 0.2

    fetched = client.get(f"/api/v1/strategy/algorithms/{created['id']}", headers=h)
    assert fetched.status_code == 200
    assert fetched.json()["params"]["C"] == 0.8

    patched = client.patch(
        f"/api/v1/strategy/algorithms/{created['id']}",
        headers=h,
        json={"name": "逻辑回归基线-v2", "params": {"C": 1.5, "max_iter": 300}},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "逻辑回归基线-v2"
    assert body["params"] == {"C": 1.5, "max_iter": 300}
    assert body["sample_id"] == sample["id"]

    assert client.delete(f"/api/v1/strategy/algorithms/{created['id']}", headers=h).status_code == 204
    assert client.delete(f"/api/v1/strategy/samples/{sample['id']}", headers=h).status_code == 204


def test_lists_are_sorted_by_name(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)

    for name in ("zeta-选股", "alpha-选股", "m-选股"):
        _create_screening(client, client.alice, ws_id, name)
    for name in ("zeta-样本", "alpha-样本", "m-样本"):
        _create_sample(client, client.alice, ws_id, name, universe_type="codes", universe_value="600519.SH")
    for name in ("zeta-算法", "alpha-算法", "m-算法"):
        _create_algorithm(client, client.alice, ws_id, name, params={})

    h = _h(client.alice, ws_id)
    assert [row["name"] for row in client.get("/api/v1/strategy/screenings", headers=h).json()] == sorted(
        ["zeta-选股", "alpha-选股", "m-选股"]
    )
    assert [row["name"] for row in client.get("/api/v1/strategy/samples", headers=h).json()] == sorted(
        ["zeta-样本", "alpha-样本", "m-样本"]
    )
    assert [row["name"] for row in client.get("/api/v1/strategy/algorithms", headers=h).json()] == sorted(
        ["zeta-算法", "alpha-算法", "m-算法"]
    )


# --------------------------------------------------------------------------- #
# 2. 角色矩阵
# --------------------------------------------------------------------------- #


def test_viewer_is_read_only_for_strategy_endpoints(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    assert _add_member(client, client.alice, ws_id, "bob", "editor") == 201
    assert _add_member(client, client.alice, ws_id, "carol", "viewer") == 201

    _seed_screening_data()
    screening = _create_screening(client, client.bob, ws_id, "只读-选股", params={"min_score": 70.0})
    sample = _create_sample(
        client, client.bob, ws_id, "只读-样本", universe_type="codes", universe_value="600519.SH,000001.SZ"
    )
    algorithm = _create_algorithm(client, client.bob, ws_id, "只读-算法", algo="logistic", params={"C": 1.0})

    h = _h(client.carol, ws_id)

    # 读端点：viewer 可以看列表与详情。
    for path in ("/api/v1/strategy/screenings", "/api/v1/strategy/samples", "/api/v1/strategy/algorithms"):
        assert client.get(path, headers=h).status_code == 200, path
    assert client.get(f"/api/v1/strategy/screenings/{screening['id']}", headers=h).status_code == 200
    assert client.get(f"/api/v1/strategy/samples/{sample['id']}", headers=h).status_code == 200
    assert client.get(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=h).status_code == 200

    # 只读求值：不写库，viewer 用来验证别人定义的策略是合理的。
    run = _run_screening(client, client.carol, ws_id, screening["id"])
    assert run["count"] == 3, run
    resolved = _resolve_sample(client, client.carol, ws_id, sample["id"])
    assert resolved["count"] == 2

    # 每一个写端点都必须挡住；光看状态码不够，还要断言只读错误头。
    write_attempts = [
        client.post("/api/v1/strategy/screenings", headers=h, json={"name": "viewer 新建选股"}),
        client.patch(f"/api/v1/strategy/screenings/{screening['id']}", headers=h, json={"description": "viewer 改的"}),
        client.delete(f"/api/v1/strategy/screenings/{screening['id']}", headers=h),
        client.post("/api/v1/strategy/samples", headers=h, json={"name": "viewer 新建样本"}),
        client.patch(f"/api/v1/strategy/samples/{sample['id']}", headers=h, json={"description": "viewer 改的"}),
        client.delete(f"/api/v1/strategy/samples/{sample['id']}", headers=h),
        client.post("/api/v1/strategy/algorithms", headers=h, json={"name": "viewer 新建算法"}),
        client.patch(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=h, json={"description": "viewer 改的"}),
        client.delete(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=h),
        client.post(f"/api/v1/strategy/algorithms/{algorithm['id']}/train", headers=h),
    ]
    for res in write_attempts:
        assert res.status_code == 403, f"{res.request.method} {res.request.url} 应为只读：{res.status_code} {res.text}"
        assert res.headers.get("X-Auth-Error") == "read_only", res.request.url

    # viewer 的请求没有产生副作用。
    assert client.get(f"/api/v1/strategy/screenings/{screening['id']}", headers=h).json()["description"] == ""


def test_editor_can_write_strategy_configs_but_not_manage_members(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    assert _add_member(client, client.alice, ws_id, "bob", "editor") == 201

    h = _h(client.bob, ws_id)

    screening = _create_screening(client, client.bob, ws_id, "editor 选股")
    assert (
        client.patch(
            f"/api/v1/strategy/screenings/{screening['id']}", headers=h, json={"params": {"min_score": 10.0}}
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/strategy/screenings/{screening['id']}", headers=h).status_code == 204

    sample = _create_sample(client, client.bob, ws_id, "editor 样本", universe_type="codes", universe_value="600519.SH")
    assert client.patch(f"/api/v1/strategy/samples/{sample['id']}", headers=h, json={"horizon": 3}).status_code == 200
    assert client.delete(f"/api/v1/strategy/samples/{sample['id']}", headers=h).status_code == 204

    algorithm = _create_algorithm(client, client.bob, ws_id, "editor 算法", params={})
    assert (
        client.patch(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=h, json={"horizon": 2}).status_code
        == 200
    )
    assert client.delete(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=h).status_code == 204

    # 成员管理是 owner 专属：editor 依然 403（test_workspace 已全覆盖，这里 spot check）。
    assert _add_member(client, client.bob, ws_id, "carol", "viewer") == 403
    assert client.patch(f"/api/v1/workspaces/{ws_id}", headers=h, json={"name": "改个名"}).status_code == 403


# --------------------------------------------------------------------------- #
# 3. 跨工作区隔离
# --------------------------------------------------------------------------- #


def test_cross_workspace_configs_are_404(strategy_client):
    client = strategy_client
    ws_a = _create_workspace(client, client.alice, name="A 工作区")
    ws_b = _create_workspace(client, client.bob, name="B 工作区")

    screening = _create_screening(client, client.alice, ws_a, "A 的选股")
    sample = _create_sample(client, client.alice, ws_a, "A 的样本", universe_type="codes", universe_value="600519.SH")
    algorithm = _create_algorithm(client, client.alice, ws_a, "A 的算法", sample_id=sample["id"])

    hb = _h(client.bob, ws_b)
    # 列表里看不到 A 的配置。
    assert client.get("/api/v1/strategy/screenings", headers=hb).json() == []
    assert client.get("/api/v1/strategy/samples", headers=hb).json() == []
    assert client.get("/api/v1/strategy/algorithms", headers=hb).json() == []

    # 直接拿 id 访问 A 的配置：读、改、删、求值、训练一律 404（不泄露 id 是否存在）。
    attempts = [
        client.get(f"/api/v1/strategy/screenings/{screening['id']}", headers=hb),
        client.patch(f"/api/v1/strategy/screenings/{screening['id']}", headers=hb, json={"name": "B 改名"}),
        client.delete(f"/api/v1/strategy/screenings/{screening['id']}", headers=hb),
        client.post(f"/api/v1/strategy/screenings/{screening['id']}/run", headers=hb),
        client.get(f"/api/v1/strategy/samples/{sample['id']}", headers=hb),
        client.patch(f"/api/v1/strategy/samples/{sample['id']}", headers=hb, json={"name": "B 改名"}),
        client.delete(f"/api/v1/strategy/samples/{sample['id']}", headers=hb),
        client.post(f"/api/v1/strategy/samples/{sample['id']}/resolve", headers=hb),
        client.get(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=hb),
        client.patch(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=hb, json={"name": "B 改名"}),
        client.delete(f"/api/v1/strategy/algorithms/{algorithm['id']}", headers=hb),
        client.post(f"/api/v1/strategy/algorithms/{algorithm['id']}/train", headers=hb),
    ]
    for res in attempts:
        assert res.status_code == 404, f"{res.request.method} {res.request.url}: {res.status_code} {res.text}"
        assert res.headers.get("X-Auth-Error") == "config_not_found", res.request.url

    # 也确认这些配置仍在原工作区可用。
    assert (
        client.get(f"/api/v1/strategy/screenings/{screening['id']}", headers=_h(client.alice, ws_a)).status_code == 200
    )


def test_cross_workspace_references_are_422_without_leaking_existence(strategy_client):
    client = strategy_client
    ws_a = _create_workspace(client, client.alice, name="A 工作区")
    ws_b = _create_workspace(client, client.bob, name="B 工作区")

    screening = _create_screening(client, client.alice, ws_a, "A 的选股")
    sample = _create_sample(client, client.alice, ws_a, "A 的样本", universe_type="codes", universe_value="600519.SH")

    # B 的样本引用 A 的选股配置：422，而不是「能解析出 A 的候选」。
    res = client.post(
        "/api/v1/strategy/samples",
        headers=_h(client.bob, ws_b),
        json={"name": "越界样本", "universe_type": "screening", "universe_value": str(screening["id"])},
    )
    assert res.status_code == 422, res.text
    assert res.headers.get("X-Auth-Error") == "screening_not_found"
    detail = res.json()["detail"]
    assert "不存在" in detail
    # 错误信息里不能出现具体 id：否则仍可通过 detail 反推出「这个 id 存在」。
    assert str(screening["id"]) not in detail

    # B 的算法引用 A 的样本：同样 422 + sample_not_found。
    res = client.post(
        "/api/v1/strategy/algorithms",
        headers=_h(client.bob, ws_b),
        json={"name": "越界算法", "algo": "logistic", "params": {"C": 1.0}, "sample_id": sample["id"]},
    )
    assert res.status_code == 422, res.text
    assert res.headers.get("X-Auth-Error") == "sample_not_found"
    assert str(sample["id"]) not in res.json()["detail"]

    # B 工作区里确实没有被偷偷创建出配置。
    assert client.get("/api/v1/strategy/samples", headers=_h(client.bob, ws_b)).json() == []
    assert client.get("/api/v1/strategy/algorithms", headers=_h(client.bob, ws_b)).json() == []


# --------------------------------------------------------------------------- #
# 4. 校验规则
# --------------------------------------------------------------------------- #


def test_duplicate_names_conflict_per_table(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    _create_screening(client, client.alice, ws_id, "重名-选股")
    dup = client.post("/api/v1/strategy/screenings", headers=h, json={"name": "重名-选股"})
    assert dup.status_code == 409
    assert dup.headers.get("X-Auth-Error") == "name_taken"

    _create_sample(client, client.alice, ws_id, "重名-样本", universe_type="codes", universe_value="600519.SH")
    dup = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={"name": "重名-样本", "universe_type": "codes", "universe_value": "600519.SH"},
    )
    assert dup.status_code == 409
    assert dup.headers.get("X-Auth-Error") == "name_taken"

    _create_algorithm(client, client.alice, ws_id, "重名-算法", params={})
    dup = client.post("/api/v1/strategy/algorithms", headers=h, json={"name": "重名-算法", "params": {}})
    assert dup.status_code == 409
    assert dup.headers.get("X-Auth-Error") == "name_taken"

    # 唯一性按「表 × 工作区」：不同类型的配置可以同名。
    assert _create_screening(client, client.alice, ws_id, "跨表同名")["name"] == "跨表同名"
    assert (
        _create_sample(client, client.alice, ws_id, "跨表同名", universe_type="codes", universe_value="600519.SH")[
            "name"
        ]
        == "跨表同名"
    )


def test_name_validation_is_422(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    # schema 层：空串 / 超长直接 422。
    assert client.post("/api/v1/strategy/screenings", headers=h, json={"name": ""}).status_code == 422
    assert client.post("/api/v1/strategy/screenings", headers=h, json={"name": "x" * 65}).status_code == 422

    # 服务层：纯空白能过 schema 的 min_length，但必须被 trim 后拒绝。
    for path in ("/api/v1/strategy/screenings", "/api/v1/strategy/samples", "/api/v1/strategy/algorithms"):
        res = client.post(path, headers=h, json={"name": "   "})
        assert res.status_code == 422, f"{path}: {res.status_code} {res.text}"
        assert res.headers.get("X-Auth-Error") == "invalid_name"

    # PATCH 也不能把名字改成空白。
    sample = _create_sample(client, client.alice, ws_id, "合法样本", universe_type="codes", universe_value="600519.SH")
    res = client.patch(f"/api/v1/strategy/samples/{sample['id']}", headers=h, json={"name": "   "})
    assert res.status_code == 422 and res.headers.get("X-Auth-Error") == "invalid_name"


def test_universe_and_window_validation_is_422(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    # 未知 universe_type 由 schema 的 Literal 拦截。
    assert (
        client.post(
            "/api/v1/strategy/samples", headers=h, json={"name": "未知来源", "universe_type": "magic"}
        ).status_code
        == 422
    )

    # 非法代码在服务层解析时拒绝。
    bad_code = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={"name": "非法代码", "universe_type": "codes", "universe_value": "NOT-A-CODE"},
    )
    assert bad_code.status_code == 422
    assert bad_code.headers.get("X-Auth-Error") == "invalid_config"

    # lookback_days 与显式日期互斥（schema 与服务层双重校验）。
    both = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={
            "name": "窗口冲突",
            "universe_type": "codes",
            "universe_value": "600519.SH",
            "lookback_days": 90,
            "start_date": "2024-01-01",
        },
    )
    assert both.status_code == 422

    reversed_window = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={
            "name": "倒置窗口",
            "universe_type": "codes",
            "universe_value": "600519.SH",
            "start_date": "2025-01-01",
            "end_date": "2024-01-01",
        },
    )
    assert reversed_window.status_code == 422

    # 空 codes 来源也必须拒绝：一个没有任何标的的样本没有意义。
    empty_codes = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={"name": "空代码", "universe_type": "codes", "universe_value": " , ; "},
    )
    assert empty_codes.status_code == 422
    assert empty_codes.headers.get("X-Auth-Error") == "invalid_universe"


def test_screening_reference_must_exist(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    res = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={"name": "引用不存在的选股", "universe_type": "screening", "universe_value": "99999"},
    )
    assert res.status_code == 422
    assert res.headers.get("X-Auth-Error") == "screening_not_found"
    assert "不存在" in res.json()["detail"]

    # 非整数 id 同样是 422，而不是 500。
    res = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={"name": "引用非法 id", "universe_type": "screening", "universe_value": "abc"},
    )
    assert res.status_code == 422
    assert res.headers.get("X-Auth-Error") == "invalid_universe"


def test_algorithm_param_validation(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    # 未知算法：schema 的 Literal 直接 422。
    assert (
        client.post("/api/v1/strategy/algorithms", headers=h, json={"name": "未知算法", "algo": "xgboost"}).status_code
        == 422
    )

    # 未知超参键：白名单校验 422，而不是等 sklearn 抛 TypeError。
    res = client.post(
        "/api/v1/strategy/algorithms",
        headers=h,
        json={"name": "未知超参", "algo": "logistic", "params": {"gamma": 1.0}},
    )
    assert res.status_code == 422
    assert res.headers.get("X-Auth-Error") == "invalid_algo_params"

    # 非数字：schema 的 dict value 类型校验。
    assert (
        client.post(
            "/api/v1/strategy/algorithms",
            headers=h,
            json={"name": "非数字超参", "algo": "logistic", "params": {"C": "high"}},
        ).status_code
        == 422
    )

    # 越界：C 的合法区间是 [1e-6, 1000]。
    res = client.post(
        "/api/v1/strategy/algorithms",
        headers=h,
        json={"name": "越界超参", "algo": "logistic", "params": {"C": 5000}},
    )
    assert res.status_code == 422
    assert res.headers.get("X-Auth-Error") == "invalid_algo_params"

    # 整数键传浮点 / 布尔值都要拒绝（bool 是 int 的子类，最容易漏）。
    for value in (200.5, True):
        res = client.post(
            "/api/v1/strategy/algorithms",
            headers=h,
            json={"name": f"类型错误-{value}", "algo": "logistic", "params": {"max_iter": value}},
        )
        assert res.status_code == 422, f"max_iter={value!r}: {res.text}"


# --------------------------------------------------------------------------- #
# 5. 选股求值
# --------------------------------------------------------------------------- #


def test_run_screening_filters_and_orders(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    _seed_screening_data()

    def run(**params) -> dict:
        config = _create_screening(client, client.alice, ws_id, f"求值-{uuid.uuid4().hex[:6]}", params=params)
        return _run_screening(client, client.alice, ws_id, config["id"])

    # 默认：排除 ST，按 score 降序。
    result = run()
    assert result["trade_date"] == str(SCREEN_TRADE_DATE)
    assert result["count"] == 4
    assert [item["code"] for item in result["items"]] == ["300750.SZ", "600519.SH", "688981.SH", "000001.SZ"]
    assert [item["name"] for item in result["items"]] == ["宁德时代", "贵州茅台", "中芯国际", "平安银行"]

    # min_score 过滤。
    result = run(min_score=75.0)
    assert [item["code"] for item in result["items"]] == ["300750.SZ", "600519.SH"]
    assert all(item["score"] >= 75.0 for item in result["items"])

    # exclude_st=false 时 ST 标的应出现（且分数最高，排第一）。
    result = run(exclude_st=False)
    assert result["count"] == 5
    assert result["items"][0]["code"] == "600000.SH"

    # boards 过滤。
    result = run(boards=["chinext"])
    assert [item["code"] for item in result["items"]] == ["300750.SZ"]

    # 逐字段条件：mom_20 >= 0.5。
    result = run(conditions=[{"field": "mom_20", "op": ">=", "value": 0.5}])
    assert [item["code"] for item in result["items"]] == ["600519.SH", "000001.SZ"]

    # limit 截断发生在排序之后（保留 score 最高的前 N 个）。
    result = run(limit=2)
    assert result["count"] == 2
    assert [item["code"] for item in result["items"]] == ["300750.SZ", "600519.SH"]

    # 升序排序。
    result = run(direction="asc", limit=2)
    assert [item["code"] for item in result["items"]] == ["000001.SZ", "688981.SH"]

    # 按 mom_20 降序：排序字段与 score 无关，必须真的换字段。
    result = run(order_by="mom_20", direction="desc")
    assert [item["code"] for item in result["items"]] == ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH"]
    assert [item["mom_20"] for item in result["items"]] == pytest.approx([1.2, 0.6, 0.4, -0.1])


def test_run_screening_without_factor_data_returns_empty(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)

    config = _create_screening(client, client.alice, ws_id, "无因子数据")
    result = _run_screening(client, client.alice, ws_id, config["id"])
    # 没跑过 analytics 应返回空结果（前端据此提示先算因子），而不是 404。
    assert result["trade_date"] is None
    assert result["count"] == 0
    assert result["items"] == []


# --------------------------------------------------------------------------- #
# 6. 样本解析
# --------------------------------------------------------------------------- #


def test_resolve_codes_universe_normalizes_and_dedupes(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    h = _h(client.alice, ws_id)

    sample = _create_sample(
        client,
        client.alice,
        ws_id,
        "代码来源",
        universe_type="codes",
        universe_value=" 600519.sh , 000001.SZ;300750.SZ\nsh600519 ",
    )
    resolved = _resolve_sample(client, client.alice, ws_id, sample["id"])
    assert resolved["codes"] == ["600519.SH", "000001.SZ", "300750.SZ"]
    assert resolved["count"] == 3
    # 未设置日期窗口时用全量历史。
    assert resolved["start"] is None and resolved["end"] is None
    assert resolved["horizon"] == 5

    # 非法代码在写入时就 422，不会静默丢弃后产生「少一只」的样本。
    bad = client.post(
        "/api/v1/strategy/samples",
        headers=h,
        json={"name": "脏代码", "universe_type": "codes", "universe_value": "600519.SH, ABC"},
    )
    assert bad.status_code == 422


def test_resolve_watchlist_is_scoped_to_workspace(strategy_client):
    client = strategy_client
    ws_a = _create_workspace(client, client.alice, name="A 工作区")
    ws_b = _create_workspace(client, client.bob, name="B 工作区")

    # 两个工作区都有同名清单，但内容不同：解析结果必须各看各的。
    for code in ("600519.SH", "000001.SZ"):
        assert (
            client.post(
                "/api/v1/watchlist", headers=_h(client.alice, ws_a), json={"code": code, "list_name": "核心组合"}
            ).status_code
            == 201
        )
    assert (
        client.post(
            "/api/v1/watchlist", headers=_h(client.bob, ws_b), json={"code": "300750.SZ", "list_name": "核心组合"}
        ).status_code
        == 201
    )
    # default 清单（空 universe_value）的兜底值就是 "default"。
    assert (
        client.post(
            "/api/v1/watchlist", headers=_h(client.alice, ws_a), json={"code": "688981.SH", "list_name": "default"}
        ).status_code
        == 201
    )

    sample_a = _create_sample(
        client, client.alice, ws_a, "A 自选", universe_type="watchlist", universe_value="核心组合"
    )
    sample_b = _create_sample(client, client.bob, ws_b, "B 自选", universe_type="watchlist", universe_value="核心组合")
    assert _resolve_sample(client, client.alice, ws_a, sample_a["id"])["codes"] == ["600519.SH", "000001.SZ"]
    assert _resolve_sample(client, client.bob, ws_b, sample_b["id"])["codes"] == ["300750.SZ"]

    sample_default = _create_sample(
        client, client.alice, ws_a, "默认清单", universe_type="watchlist", universe_value=""
    )
    resolved = _resolve_sample(client, client.alice, ws_a, sample_default["id"])
    assert resolved["codes"] == ["688981.SH"]
    # 存储值被规范化为清单名，避免「空字符串」在求值时变成隐式语义。
    assert sample_default["universe_value"] == "default"


def test_resolve_screening_reuses_screening_config(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    _seed_screening_data()

    screening = _create_screening(
        client, client.alice, ws_id, "解析用选股", params={"min_score": 75.0, "boards": ["main", "chinext"]}
    )
    sample = _create_sample(
        client,
        client.alice,
        ws_id,
        "引用选股",
        universe_type="screening",
        universe_value=str(screening["id"]),
    )
    assert sample["universe_value"] == str(screening["id"])

    run = _run_screening(client, client.alice, ws_id, screening["id"])
    resolved = _resolve_sample(client, client.alice, ws_id, sample["id"])
    # 样本解析与直接运行选股必须得到同一份候选池（同一个配置，不能有第二套语义）。
    assert resolved["codes"] == [item["code"] for item in run["items"]]
    assert resolved["codes"] == ["300750.SZ", "600519.SH"]
    assert resolved["count"] == 2


def test_resolve_default_universe_falls_back_to_instruments(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    codes = ["600519.SH", "000001.SZ", "300750.SZ"]
    _seed_instruments(codes)
    _seed_bars(codes)

    sample = _create_sample(client, client.alice, ws_id, "默认宇宙", universe_type="default")
    resolved = _resolve_sample(client, client.alice, ws_id, sample["id"])
    # 没有自选、没有指数成分时回退到「所有有行情数据的已上市标的」。
    assert sorted(resolved["codes"]) == sorted(codes)
    assert resolved["count"] == 3
    assert sample["universe_value"] == ""


def test_resolve_explicit_window_and_lookback(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    _seed_bars(["600519.SH"], start=date(2024, 1, 1), end=date(2024, 12, 31))

    explicit = _create_sample(
        client,
        client.alice,
        ws_id,
        "显式窗口",
        universe_type="codes",
        universe_value="600519.SH",
        start_date="2024-01-01",
        end_date="2024-06-30",
        horizon=5,
    )
    resolved = _resolve_sample(client, client.alice, ws_id, explicit["id"])
    assert (resolved["start"], resolved["end"]) == ("2024-01-01", "2024-06-30")
    assert resolved["horizon"] == 5

    # lookback_days 以库中最新交易日为终点向前推，而不是以「今天」为准。
    lookback = _create_sample(
        client,
        client.alice,
        ws_id,
        "回看窗口",
        universe_type="codes",
        universe_value="600519.SH",
        lookback_days=90,
        horizon=3,
    )
    resolved = _resolve_sample(client, client.alice, ws_id, lookback["id"])
    assert resolved["end"] == "2024-12-31"
    assert resolved["start"] == str(date(2024, 12, 31) - timedelta(days=90))
    assert resolved["horizon"] == 3


def test_resolve_min_bars_does_not_affect_count(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)

    low = _create_sample(
        client, client.alice, ws_id, "低门槛", universe_type="codes", universe_value="600519.SH,000001.SZ", min_bars=10
    )
    high = _create_sample(
        client,
        client.alice,
        ws_id,
        "高门槛",
        universe_type="codes",
        universe_value="600519.SH,000001.SZ",
        min_bars=2500,
    )
    # min_bars 只影响训练时「每只标的历史够不够」，样本解析不改标的数量。
    assert _resolve_sample(client, client.alice, ws_id, low["id"])["count"] == 2
    assert _resolve_sample(client, client.alice, ws_id, high["id"])["count"] == 2


# --------------------------------------------------------------------------- #
# 7. 算法训练
# --------------------------------------------------------------------------- #


def test_train_logistic_algorithm_registers_model(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    codes = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH"]
    _seed_bars(codes, start=date(2024, 1, 1), end=date(2025, 12, 31))

    sample = _create_sample(
        client,
        client.alice,
        ws_id,
        "训练样本",
        universe_type="codes",
        universe_value=",".join(codes),
        start_date="2024-01-01",
        end_date="2025-12-31",
        horizon=1,
        min_bars=120,
    )
    algorithm = _create_algorithm(
        client,
        client.alice,
        ws_id,
        "逻辑回归训练",
        algo="logistic",
        params={"max_iter": 200, "C": 0.5},
        sample_id=sample["id"],
        horizon=1,
        test_fraction=0.2,
    )

    res = client.post(
        f"/api/v1/strategy/algorithms/{algorithm['id']}/train",
        headers=_h(client.alice, ws_id),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["model_id"], body
    assert body["algo"] == "logistic"
    assert body["horizon"] == 1
    assert body["n_train"] > 0 and body["n_test"] > 0
    assert "auc" in body["metrics"] and body["metrics"]["auc"] is not None, body["metrics"]

    # 训练产物必须写进全局模型注册表并置为 active，否则预测阶段找不到它。
    from app.db import session_scope
    from app.db.models import ModelRegistry

    with session_scope() as session:
        row = session.get(ModelRegistry, body["model_id"])
        assert row is not None, "训练后 model_registry 必须有对应行"
        assert row.is_active is True
        assert row.algo == "logistic"
        assert row.horizon == 1
        assert row.n_train == body["n_train"] and row.n_test == body["n_test"]
        # 超参写进注册表，才能复现同一个配置训练出的模型。
        params = json.loads(row.params_json)
        assert params["algo"] == "logistic"
        assert params["params"]["max_iter"] == 200
        assert params["params"]["C"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 8. 工作区集成：删除统计 / 未归属数据认领
# --------------------------------------------------------------------------- #


def test_workspace_purge_counts_strategy_configs(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    sample = _create_sample(
        client, client.alice, ws_id, "待清理样本", universe_type="codes", universe_value="600519.SH"
    )
    _create_screening(client, client.alice, ws_id, "待清理选股")
    _create_algorithm(client, client.alice, ws_id, "待清理算法", sample_id=sample["id"])

    res = client.delete(f"/api/v1/workspaces/{ws_id}?purge=true", headers=_h(client.alice))
    assert res.status_code == 200, res.text
    purged = res.json()["purged"]
    assert purged["screening_configs"] == 1
    assert purged["sample_configs"] == 1
    assert purged["algorithm_configs"] == 1

    from app.db import session_scope
    from app.db.models import AlgorithmConfig, SampleConfig, ScreeningConfig, Workspace

    with session_scope() as session:
        assert session.get(Workspace, ws_id) is None
        assert session.query(ScreeningConfig).filter_by(workspace_id=ws_id).count() == 0
        assert session.query(SampleConfig).filter_by(workspace_id=ws_id).count() == 0
        assert session.query(AlgorithmConfig).filter_by(workspace_id=ws_id).count() == 0


def _make_unassigned_configs() -> dict[str, int]:
    """直接走服务层在 workspace_id=0（隔离前的共享桶）造三张表的配置。"""
    from app.db import session_scope
    from app.schemas import AlgorithmConfigIn, SampleConfigIn, ScreeningConfigIn
    from app.strategy import service

    with session_scope() as session:
        screening = service.create_screening(
            session, 0, user_id=None, payload=ScreeningConfigIn(name="无主选股", params={"min_score": 60.0})
        )
        sample = service.create_sample(
            session,
            0,
            user_id=None,
            payload=SampleConfigIn(name="无主样本", universe_type="codes", universe_value="600519.SH"),
        )
        algorithm = service.create_algorithm(
            session,
            0,
            user_id=None,
            payload=AlgorithmConfigIn(name="无主算法", algo="logistic", params={"C": 1.0}, sample_id=sample.id),
        )
    return {"screening_id": screening.id, "sample_id": sample.id, "algorithm_id": algorithm.id}


def test_claim_unassigned_moves_strategy_configs(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    _make_unassigned_configs()

    from app.db import session_scope
    from app.db.models import AlgorithmConfig, SampleConfig, ScreeningConfig
    from app.workspace.service import claim_unassigned

    # dry_run：只统计、不写库。
    with session_scope() as session:
        preview = claim_unassigned(session, ws_id, dry_run=True)
    assert preview["screening_configs"] == 1
    assert preview["sample_configs"] == 1
    assert preview["algorithm_configs"] == 1
    assert preview["duplicates_skipped"] == 0
    with session_scope() as session:
        assert session.query(ScreeningConfig).filter_by(workspace_id=0).count() == 1
        assert session.query(SampleConfig).filter_by(workspace_id=ws_id).count() == 0

    # 真正搬运：三张表都换到目标工作区。
    with session_scope() as session:
        counts = claim_unassigned(session, ws_id)
    assert counts == preview
    with session_scope() as session:
        assert session.query(ScreeningConfig).filter_by(workspace_id=0).count() == 0
        assert session.query(SampleConfig).filter_by(workspace_id=0).count() == 0
        assert session.query(AlgorithmConfig).filter_by(workspace_id=0).count() == 0
        screening = session.query(ScreeningConfig).filter_by(workspace_id=ws_id).one()
        sample = session.query(SampleConfig).filter_by(workspace_id=ws_id).one()
        algorithm = session.query(AlgorithmConfig).filter_by(workspace_id=ws_id).one()
        assert screening.name == "无主选股"
        assert sample.name == "无主样本"
        # 算法对样本的引用不应在搬运中断开。
        assert algorithm.sample_id == sample.id


def test_claim_unassigned_duplicate_dry_run_and_conflict_skipping(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    # 目标工作区先有一条同名选股配置：认领时不能撞唯一约束，也不能覆盖用户已有的版本。
    target = _create_screening(client, client.alice, ws_id, "共享选股", params={"min_score": 90.0})

    from app.db import session_scope
    from app.db.models import ScreeningConfig
    from app.schemas import ScreeningConfigIn
    from app.strategy import service
    from app.workspace.service import claim_unassigned

    with session_scope() as session:
        service.create_screening(
            session, 0, user_id=None, payload=ScreeningConfigIn(name="共享选股", description="旧的无主副本")
        )
        service.create_screening(session, 0, user_id=None, payload=ScreeningConfigIn(name="独有选股"))

    with session_scope() as session:
        preview = claim_unassigned(session, ws_id, dry_run=True)
    assert preview["screening_configs"] == 1
    assert preview["duplicates_skipped"] == 1

    with session_scope() as session:
        counts = claim_unassigned(session, ws_id)
    assert counts["screening_configs"] == 1
    assert counts["duplicates_skipped"] == 1

    with session_scope() as session:
        rows = session.query(ScreeningConfig).filter_by(workspace_id=ws_id).all()
        assert sorted(row.name for row in rows) == ["共享选股", "独有选股"]
        surviving = next(row for row in rows if row.name == "共享选股")
        # 保留的是目标工作区自己的版本（参数与描述都没被旧副本覆盖）。
        assert surviving.id == target["id"]
        assert surviving.description == ""
        assert json.loads(surviving.params_json)["min_score"] == 90.0
        assert session.query(ScreeningConfig).filter_by(workspace_id=0).count() == 0


# --------------------------------------------------------------------------- #
# 9. 回测集成
# --------------------------------------------------------------------------- #


def test_backtest_uses_screening_config_for_universe(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    _seed_screening_data()
    _seed_bars(["300750.SZ", "600519.SH"], start=date(2024, 1, 1), end=date(2025, 6, 30))

    screening = _create_screening(client, client.alice, ws_id, "回测选股", params={"min_score": 75.0})
    res = client.post(
        "/api/v1/backtest/run",
        headers=_h(client.alice, ws_id),
        json={"strategy": "buy_hold", "screening_id": screening["id"], "benchmark": None},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["run_id"]
    assert body["equity"], "只命中部分标的时回测也必须产出净值曲线"
    assert body["metrics"]["n_trades"] > 0

    from app.db import session_scope
    from app.db.models import BacktestRun

    with session_scope() as session:
        run = session.get(BacktestRun, body["run_id"])
        assert run is not None
        assert run.workspace_id == ws_id
        params = json.loads(run.params_json)
        # 记录 screening_id 才能解释「这次回测的标的是哪来的」。
        assert params["screening_id"] == screening["id"]
        assert params["codes"] == []


def test_backtest_no_screening_candidates_returns_409(strategy_client):
    client = strategy_client
    ws_id = _create_workspace(client, client.alice)
    _seed_screening_data()
    _seed_bars(["600519.SH"], start=date(2024, 1, 1), end=date(2024, 6, 30))

    screening = _create_screening(client, client.alice, ws_id, "不可能命中的选股", params={"min_score": 1000.0})
    res = client.post(
        "/api/v1/backtest/run",
        headers=_h(client.alice, ws_id),
        json={"strategy": "buy_hold", "screening_id": screening["id"], "benchmark": None},
    )
    # 不能静默回退默认宇宙：那会让用户以为在测自己的策略。
    assert res.status_code == 409, res.text
    assert "no candidates" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# 10. 隔离关闭的旧共享语义
# --------------------------------------------------------------------------- #


def test_shared_mode_keeps_legacy_semantics(shared_client):
    """隔离关闭时：无需 X-Workspace-Id，配置落在未归属桶 workspace_id=0。"""
    client = shared_client

    created = client.post(
        "/api/v1/strategy/screenings",
        json={"name": "共享模式选股", "params": {"min_score": 70.0}},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["workspace_id"] == 0

    # 不带任何工作区头也能列出同一条配置（旧的单机行为）。
    rows = client.get("/api/v1/strategy/screenings")
    assert rows.status_code == 200
    assert [row["name"] for row in rows.json()] == ["共享模式选股"]

    # 重名仍然是 409；样本在共享模式下也能创建并解析。
    assert client.post("/api/v1/strategy/screenings", json={"name": "共享模式选股"}).status_code == 409

    sample = client.post(
        "/api/v1/strategy/samples",
        json={"name": "共享模式样本", "universe_type": "codes", "universe_value": "600519.SH,000001.SZ"},
    )
    assert sample.status_code == 201
    assert sample.json()["workspace_id"] == 0
    resolved = client.post(f"/api/v1/strategy/samples/{sample.json()['id']}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["count"] == 2
