"""tpcore 顶底识别模型的单元与集成测试。

重点守护三条口径（移植自量析平台时最容易漂移的地方）：

1. **无未来函数**：截断未来数据后，历史行的全部 ``tp_*`` 列必须逐值不变；
2. **事件去重**：冷却期内反复穿越阈值只保留首个候选；
3. **右侧确认**：候选后必须出现「收阳且不破前一日低点」才产生买入信号。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stockmaster.core.tpcore import (
    DEFAULT_PARAMS,
    bottom_score,
    build,
    feature_frame,
    sector_index,
    top_count,
    zigzag,
)


def _ohlc(prices: pd.Series) -> pd.DataFrame:
    """由收盘价序列构造 OHLCV（高低围绕收盘 ±0.5%，开=前收）。"""
    prev = prices.shift(1).fillna(prices)
    high = np.maximum(prices, prev) * 1.005
    low = np.minimum(prices, prev) * 0.995
    return pd.DataFrame(
        {
            "date": prices.index.astype(str),
            "open": prev.values,
            "high": high.values,
            "low": low.values,
            "close": prices.values,
            "volume": np.linspace(1e6, 2e6, len(prices)),
        }
    )


def _mean_reverting_series(n: int = 600, seed: int = 7) -> pd.Series:
    """带均值回归的日线收盘价：构造足够多的底部/顶部穿越。"""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(steps) - np.linspace(0, 1.2, n))
    # 叠加缓慢正弦，制造可识别的波段
    close = close * (1 + 0.08 * np.sin(np.linspace(0, 8 * np.pi, n)))
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    return pd.Series(close, index=idx)


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return _ohlc(_mean_reverting_series())


# ── 无未来函数 ────────────────────────────────────────────────────────────


def test_no_lookahead(frame):
    """截断未来数据后历史值不变——模型的核心正确性断言。"""
    full = build(frame)
    cut = len(frame) // 2
    part = build(frame.iloc[:cut].reset_index(drop=True))
    cols = [c for c in full.columns if c.startswith("tp_")]
    for col in cols:
        a = full[col].iloc[:cut].reset_index(drop=True)
        b = part[col].reset_index(drop=True)
        pd.testing.assert_series_equal(a, b, check_names=False, check_dtype=False)


def test_no_lookahead_features(frame):
    full = feature_frame(frame)
    cut = len(frame) // 2
    part = feature_frame(frame.iloc[:cut].reset_index(drop=True))
    for col in full.columns:
        pd.testing.assert_series_equal(
            full[col].iloc[:cut].reset_index(drop=True),
            part[col].reset_index(drop=True),
            check_names=False,
            check_dtype=False,
        )


# ── 特征方向 ──────────────────────────────────────────────────────────────


def test_bottom_and_top_directions_oppose():
    """单边下跌的尾部底部分更高；单边上涨的尾部顶部计数更高。"""
    n = 500
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    down = pd.Series(np.linspace(100, 40, n) * (1 + np.sin(np.linspace(0, 20, n)) * 0.001), index=idx)
    up = pd.Series(np.linspace(40, 100, n) * (1 + np.sin(np.linspace(0, 20, n)) * 0.001), index=idx)
    feat_down = feature_frame(_ohlc(down))
    feat_up = feature_frame(_ohlc(up))
    assert bottom_score(feat_down).iloc[-1] > bottom_score(feat_up).iloc[-1]
    assert top_count(feat_up).iloc[-1] > top_count(feat_down).iloc[-1]


# ── 冷却去重 ──────────────────────────────────────────────────────────────


def test_cooldown_dedupes_repeated_crossings():
    """阈值上下反复穿越只保留冷却间隔后的首个候选。"""
    thr = DEFAULT_PARAMS["thr"]
    bs = pd.Series([thr - 1, thr, thr + 1, thr - 1, thr + 1, thr - 1, thr + 1], dtype=float)
    raw = (bs >= thr) & (bs.shift(1) < thr)
    raw = raw.fillna(False).astype(bool)
    from stockmaster.core.tpcore import _cooldown

    cand = _cooldown(raw, 6)
    assert int(cand.sum()) == 1  # 第 1 与第 4 次穿越间隔 < 6，被合并


def test_entry_requires_right_side_confirm():
    """候选后无阳线确认不产生 entry；出现收阳不破前低当日产生 entry。"""
    n = 10
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    flat = pd.Series([10.0] * n, index=idx)
    d = _ohlc(flat)
    cand = pd.Series([False] * n, index=idx)
    cand.iloc[2] = True
    from stockmaster.core.tpcore import _confirm

    # 阴线（close < open）不确认
    d_down = d.copy()
    d_down.loc[d_down.index[3], "close"] = d_down.loc[d_down.index[3], "open"] * 0.99
    out_down = _confirm(d_down, cand, 3)
    assert not out_down.any()
    # 阳线且不破前一日低点 → 确认
    d_up = d.copy()
    d_up.loc[d_up.index[4], "close"] = d_up.loc[d_up.index[4], "open"] * 1.02
    d_up.loc[d_up.index[4], "low"] = d_up.loc[d_up.index[4], "open"] * 0.999
    out_up = _confirm(d_up, cand, 3)
    assert bool(out_up.iloc[4]) and int(out_up.sum()) == 1


def test_gate_suspends_new_low_collapse():
    """20 日跌超 25% 且当日创新低 → 闸门挂起（-1），候选被抑制。"""
    n = 320
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    crash = pd.Series(100.0 * 0.984 ** np.arange(n), index=idx)  # 几何阴跌：20 日跌 >25%
    d = _ohlc(crash)
    out = build(d, {"gate": True})
    assert (out["tp_gate"] == -1).any()
    assert not out["tp_cand"].any() or (not out.loc[out["tp_gate"] == -1, "tp_cand"]).all()


def test_sector_index_alignment():
    """sector_index 按日期串对齐，成分过少时返回空帧。"""
    idx = pd.date_range("2023-01-02", periods=400, freq="B")
    f1 = _ohlc(pd.Series(np.linspace(10, 12, 400), index=idx))
    f2 = _ohlc(pd.Series(np.linspace(5, 6, 400), index=idx))
    f3 = _ohlc(pd.Series(np.linspace(20, 18, 400), index=idx))
    sec = sector_index(["a", "b", "c"], {"a": f1, "b": f2, "c": f3})
    assert not sec.empty and {"sret20", "spos250"} <= set(sec.columns)
    # 成分不足（<3 只有效）时按量析口径返回空帧
    assert sector_index(["a", "b"], {"a": f1, "b": f2}).empty


def test_zigzag_finds_pivots():
    """锯齿行情里 ZigZag 标出高低拐点，且方向交替。"""
    n = 400
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    wave = pd.Series(100 + 15 * np.sin(np.linspace(0, 6 * np.pi, n)), index=idx)
    d = _ohlc(wave)
    piv = zigzag(d, pct=0.08)
    assert len(piv) >= 4
    kinds = [k for _i, k, _v in piv]
    assert all(kinds[i] != kinds[i + 1] for i in range(len(kinds) - 1))


# ── API 集成 ──────────────────────────────────────────────────────────────


def _business_days(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def turning_client(tmp_path, monkeypatch):
    """临时库 + 两只标的多轮波段行情，走真实插件路由。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"turning-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    from stockmaster.db import session_scope
    from stockmaster.providers.base import Bar, InstrumentInfo
    from stockmaster.sync import repository as repo

    days = _business_days(420)
    with session_scope() as session:
        repo.upsert_calendar(session, days, source="test")
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code="600900.SH", name="长江电力", market="SH", board="main", status=1),
                InstrumentInfo(code="600886.SH", name="国投电力", market="SH", board="main", status=1),
            ],
        )
        rng = np.random.default_rng(11)
        for code, base in (("600900.SH", 24.0), ("600886.SH", 12.0)):
            bars = []
            price = base
            for day in days:
                # 正弦波段 + 微噪声：产生足够的阈值穿越
                wave = 1 + 0.10 * np.sin(len(bars) / 18.0)
                price = base * wave * (1 + rng.normal(0, 0.002))
                prev = base * (1 + 0.10 * np.sin((len(bars) - 1) / 18.0))
                bars.append(
                    Bar(
                        code=code,
                        trade_date=day,
                        open=float(prev),
                        high=float(price * 1.004),
                        low=float(price * 0.996),
                        close=float(price),
                        volume=float(1e6 + len(bars) * 1e3),
                        amount=float(price * 1e6),
                        source="mock",
                    )
                )
            repo.upsert_daily_bars(session, code, bars, "qfq")

    from stockmaster.main import create_app

    client = TestClient(create_app())
    yield client
    session_mod.reset_engine()
    get_settings.cache_clear()


def test_scan_api(turning_client):
    resp = turning_client.get("/api/v1/turning/scan", params={"codes": "600900.SH,600886.SH"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["trade_date"]
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert item["code"] == "600900.SH"
    assert item["gate"] in (1, 0, -1)
    assert set(item["percentiles"]) == {"pos250", "bias60", "dd250", "rsi14", "rsi6", "bb_pos", "atr_pct"}


def test_scan_api_missing_and_validation(turning_client):
    resp = turning_client.get("/api/v1/turning/scan", params={"codes": "600900.SH,999999.SH"})
    assert resp.status_code == 200
    assert resp.json()["missing"] == ["999999.SH"]

    resp = turning_client.get("/api/v1/turning/scan", params={"codes": ""})
    assert resp.status_code == 422


def test_history_api(turning_client):
    resp = turning_client.get("/api/v1/turning/history/600900.SH", params={"kind": "bottom"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "600900.SH"
    for event in body["events"]:
        assert event["kind"] == "bottom"
        assert set(event) >= {"trade_date", "close", "score", "gate", "entry"}


def test_rules_api(turning_client):
    resp = turning_client.get("/api/v1/turning/rules")
    assert resp.status_code == 200
    body = resp.json()
    assert body["buy"] and body["note"]
