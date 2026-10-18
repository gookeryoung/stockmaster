"""A 类债核心顶底识别模型（turning-point core for bond-like core assets）。

移植自量析平台 ``backend/tpcore.py``，方法论口径逐项对齐，实现适配本项目的
指标体系（:mod:`stockmaster.indicators.compute`，RSI 用 Wilder 平滑、BOLL 用
总体标准差，与行情终端一致）。

适用对象：低波动、低换手、与大盘低相关的类债核心资产（长江电力、银行、高速、
港口、核电）。这类标的的核心统计特征是「20 日动量 IC 显著为负」，即价格对短期
偏离有均值回归倾向。因此模型只做反转，不做动量。

与通用「综合评分模型」的根本区别::

    通用模型   = 固定阈值判据加权求和（评分 ≥ 阈值建仓），假设所有标的同质
    本模型     = 滚动分位标定 + 最短板聚合 + 环境闸门 + 右侧确认

分层（对应量析落地清单的 7 项）：

    1 数据层  交由本项目 sync 层的前复权重建守护（跳变 >25% 校验），本模块只
              接受干净序列
    2 标注层  ZigZag 10% + 窗口 [-15, +3]，仅用于验证脚本（scripts/tp_validate.py），
              不进入生产信号
    3 特征层  七维全部走滚动 250 日分位（min_periods=120），不用固定阈值
    4 聚合层  底部取「最短板」（七维分位的最小值），顶部取「共振计数」（≥90 的项数）
    5 验证层  事件级去重（冷却期），只看事件首日；时间切分 70/30（验证脚本）
    6 闸门层  板块 20 日跌幅 < −10% 或板块 250 日位置 < 15 → 降档（阈值上浮 10 分）；
              个股仍在创新低 → 挂起
    7 确认层  共振分只给候选，买入需 3 日内出现「不破前低的阳线」

为什么底部必须用「最短板」而不是加权平均：平均会把「六个平庸 + 一个极端」这种
最常见的假底稀释成中等分；最短板要求七个维度同时到位，天然过滤掉单维度超卖。
量析实测（电力组 21 只、2020-08~2026-10）：最短板 ≥75 占 1.8% 交易日、命中 74%；
共振计数 ≥4 占 7.5% 却只有 57% 命中——计数会被连续同向下跌灌满，所以底部绝不能
用计数，顶部才用。

无前视保证：所有特征只用当期及之前的数据；分位用 ``rolling().rank(pct=True)``
（C 实现，快两个数量级，与逐行 mean 比较实测最大差 < 1 分位点）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stockmaster.indicators.compute import atr, boll, ma, rsi, volume_ratio

WIN = 250
MINP = 120
TOP_Q = 90  # 顶部共振计数阈值（分位）
TOP_CNT = 4  # 顶部信号需要几项共振
POOL_GAP = 25  # 事件去重的最小间隔（交易日，验证口径）
CONFIRM_DAYS = 3  # 右侧确认窗口

__all__ = [
    "CONFIRM_DAYS",
    "DEFAULT_PARAMS",
    "FEATURE_DEFS",
    "MINP",
    "POOL_GAP",
    "TOP_CNT",
    "TOP_Q",
    "WIN",
    "base_frame",
    "bottom_score",
    "build",
    "describe",
    "entry",
    "feature_frame",
    "sector_index",
    "top_count",
    "zigzag",
]

# (原始列名, 方向, 中文名)  方向 reverse = 值越低越接近底部；forward = 值越高越接近底部
FEATURE_DEFS = [
    ("pos250", "reverse", "250 日区间位置"),
    ("bias60", "reverse", "60 日乖离"),
    ("dd250", "reverse", "距 250 日高点跌幅"),
    ("rsi14", "reverse", "RSI14"),
    ("rsi6", "reverse", "RSI6"),
    ("bb_pos", "reverse", "布林带位置"),
    ("atr_pct", "forward", "波动分位"),
]

DEFAULT_PARAMS = {
    "win": WIN,
    "minp": MINP,
    "thr": 75,  # 底部分位阈值
    "top_cnt": TOP_CNT,  # 顶部共振项数
    "gate": True,  # 环境闸门
    "confirm": CONFIRM_DAYS,
}


def base_frame(d: pd.DataFrame) -> pd.DataFrame:
    """计算七维特征需要的原始列（在复权 OHLCV 之上）。

    ``d`` 需要含 ``open/high/low/close/volume`` 列、按日期升序排列；
    返回追加列的副本。全部为回看计算，无前视。
    """
    out = d.copy()
    c = out["close"].astype(float)
    high = out["high"].astype(float) if "high" in out else c
    low = out["low"].astype(float) if "low" in out else c
    volume = out["volume"].astype(float) if "volume" in out else pd.Series(0.0, index=out.index)

    out["ma60"] = ma(c, 60)
    out["rsi6"] = rsi(c, 6)
    out["rsi14"] = rsi(c, 14)
    bb = boll(c)
    span = (bb["up"] - bb["low"]).replace(0.0, np.nan)
    out["bb_pos"] = (c - bb["low"]) / span * 100.0
    out["atr_pct"] = atr(high, low, c, 14) / c.replace(0.0, np.nan) * 100.0
    out["hh250"] = c.rolling(250).max()
    out["ll250"] = c.rolling(250).min()
    out["ll20"] = c.rolling(20).min()
    out["vol_ratio20"] = volume_ratio(volume, 5)
    out["pos250"] = (c - out["ll250"]) / (out["hh250"] - out["ll250"]).replace(0.0, np.nan) * 100.0
    out["bias60"] = (c / out["ma60"] - 1.0) * 100.0
    out["dd250"] = (c / out["hh250"] - 1.0) * 100.0
    return out


def rolling_pct(s: pd.Series, win: int = WIN, minp: int = MINP) -> pd.Series:
    """滚动历史分位（0-100，只用当期及之前，无前视）。"""
    return s.rolling(win, min_periods=minp).rank(pct=True) * 100.0


def feature_frame(d: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """七维分位（0-100，越大越接近底部）。

    返回列：``p_pos250`` / ``p_bias60`` / ...
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    win, minp = int(p["win"]), int(p["minp"])
    b = base_frame(d) if "ma60" not in d.columns else d
    raw = {
        "pos250": b["pos250"],
        "bias60": b["bias60"],
        "dd250": b["dd250"],
        "rsi14": b["rsi14"],
        "rsi6": b["rsi6"],
        "bb_pos": b["bb_pos"],
        "atr_pct": b["atr_pct"],
    }
    out = pd.DataFrame(index=b.index)
    for key, direction, _label in FEATURE_DEFS:
        q = rolling_pct(raw[key], win, minp)
        out[f"p_{key}"] = q if direction == "forward" else (100.0 - q)
    return out


def bottom_score(feat: pd.DataFrame) -> pd.Series:
    """底部综合分 = 七维分位取最小值（最短板）。"""
    cols = [f"p_{k}" for k, _d, _l in FEATURE_DEFS]
    return feat[cols].min(axis=1)


def top_count(feat: pd.DataFrame, thr: float = TOP_Q) -> pd.Series:
    """顶部共振计数：七维里有几项同时进入最极端分位。

    注意方向相反：底部用 ``100 - q``（越低越接近底），顶部要用 ``q``（越高越接近顶）。
    """
    cols = [f"p_{k}" for k, _d, _l in FEATURE_DEFS]
    rev = 100.0 - feat[cols]
    return (rev >= thr).sum(axis=1)


def _gate_series(d: pd.DataFrame, sector: pd.DataFrame | None, p: dict) -> pd.Series:
    """环境闸门：1 = 正常，0 = 降档，-1 = 挂起。

    闸门不参与打分，只调节信噪。理由（量析实测）：2026 年 6–7 月电力板块系统性
    下跌时，个股超级共振信号连续误报且越报越低（每根阴线都让七个维度更极端一分），
    打分层面无法自救，只能在环境层面否决。
    """
    state = pd.Series(1, index=d.index, dtype=int)
    # 个股仍在创新低：20 日跌超 25% 且当日就是 20 日新低 → 信号挂起。
    # 该判据只依赖个股自身，与是否提供板块指数无关（量析原实现把它放在
    # sector 分支内，板块缺失时挂起失效；移植时修正为始终生效）。
    c = d["close"]
    dd20 = (c / c.rolling(20).max() - 1.0) * 100.0
    ll20 = d["ll20"] if "ll20" in d else c.rolling(20).min()
    state = state.mask((dd20 < -25.0) & (c <= ll20), -1)
    if not p.get("gate") or sector is None or len(sector) == 0:
        return state
    try:
        if "date" in d.columns:
            sec = sector.reindex(d["date"].astype(str))
        else:
            sec = sector.reindex(d.index.astype(str))
    except (KeyError, TypeError):
        return state
    sec.index = d.index
    sec = sec.ffill()

    if "sret20" in sec:
        state = state.mask(sec["sret20"].fillna(0.0) < -10.0, 0)
    if "spos250" in sec:
        state = state.mask(sec["spos250"].fillna(50.0) < 15.0, 0)
    return state


def _confirm(d: pd.DataFrame, cand: pd.Series, days: int) -> pd.Series:
    """右侧确认：候选日后 ``days`` 个交易日内出现「收阳且不破前一日低点」。

    左侧识别解决「便不便宜」，右侧确认解决「还会不会更便宜」。
    两者是两件事，不能合并成一个打分。
    """
    if days < 0:
        # 消融用：不做右侧确认，候选日即为买点
        return cand.copy()
    c, o, lo = d["close"].values, d["open"].values, d["low"].values
    safe = (c > o) & (lo >= np.r_[lo[0], lo[:-1]])
    idx = np.where(cand.fillna(False).values)[0]
    out = np.zeros(len(d), dtype=bool)
    for i in idx:
        for k in range(0, days + 1):
            j = i + k
            if j >= len(d):
                break
            if safe[j]:
                out[j] = True
                break
    return pd.Series(out, index=d.index)


def _cooldown(raw: pd.Series, cool: int) -> pd.Series:
    """候选之间至少间隔 ``cool`` 个交易日。

    没有冷却期时，一轮连续下跌会在阈值上下反复穿越，同一波底部被拆成 3~4 次信号
    （量析实测：长江电力 2022-03 被拆成 03-15 / 03-17 / 03-24 三次），事件统计会
    把同一笔机会数成多笔，虚增信号次数。
    """
    idx = np.where(raw.fillna(False).values)[0]
    keep: list[int] = []
    last = -(10**9)
    for i in idx:
        if i - last >= cool:
            keep.append(i)
            last = i
    out = pd.Series(False, index=raw.index)
    if keep:
        out.iloc[keep] = True
    return out


def build(
    d: pd.DataFrame,
    params: dict | None = None,
    sector: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """计算全部分层结果，返回带 ``tp_*`` 列的新 DataFrame。

    列含义：

        tp_p_*      七维分位（0-100，越大越接近底部）
        tp_bottom   底部综合分（最短板）
        tp_top      顶部共振计数
        tp_cand     候选（底部首次达到阈值，已做冷却去重）
        tp_gate     闸门状态 1 正常 / 0 降档 / -1 挂起
        tp_entry    最终买入信号（候选 + 闸门 + 确认）
        tp_hold_thr 生效阈值（降档时上浮 10 分）
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    feat = feature_frame(d, p)
    out = d.copy()
    for col in feat.columns:
        out[f"tp_{col}"] = feat[col]
    out["tp_bottom"] = bottom_score(feat)
    out["tp_top"] = top_count(feat)

    gate = _gate_series(d, sector, p)
    out["tp_gate"] = gate
    thr = float(p["thr"])
    eff = pd.Series(np.where(gate <= 0, thr + 10.0, thr), index=d.index, dtype=float)
    out["tp_hold_thr"] = eff

    bs = out["tp_bottom"]
    raw = (bs >= eff) & (bs.shift(1) < eff)
    raw = raw.fillna(False).astype(bool) & (gate >= 0)
    cand = _cooldown(raw, int(p["confirm"]) + 2)
    out["tp_cand"] = cand
    out["tp_entry"] = _confirm(d, cand, int(p["confirm"]))
    return out


def entry(
    d: pd.DataFrame,
    params: dict | None = None,
    sector: pd.DataFrame | None = None,
) -> pd.Series:
    """只要买入信号（供策略信号层调用）。"""
    return build(d, params, sector)["tp_entry"].fillna(False)


def sector_index(codes: list[str], frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """类债核心等权板块指数：``sret20``（20 日收益%）与 ``spos250``（250 日位置）。

    ``frames`` 是 ``{code: 已加指标的 DataFrame}``，按日期对齐后等权平均。
    对齐时只保留「至少一半成分有数据」的交易日：同行业里常有数据很短的次新股，
    若直接 ``dropna(how="any")`` 会把整段历史砍掉，若用 ``dropna(how="all")``
    又会让指数出现大段空洞、``shift(20)`` 直接错位。
    """
    closes: list[pd.Series] = []
    for code in codes:
        d = frames.get(code)
        if d is None or len(d) < 300:
            continue
        date_key = d["date"].astype(str) if "date" in d else pd.Series(d.index.astype(str), index=d.index)
        s = pd.Series(d["close"].values, index=date_key)
        s = s[~s.index.duplicated(keep="last")]
        closes.append(s)
    if not closes:
        return pd.DataFrame()
    px = pd.concat(closes, axis=1).sort_index()
    px = px[px.notna().sum(axis=1) >= max(3, len(closes) // 2)]
    px = px.mean(axis=1).dropna()
    if len(px) < 300:
        return pd.DataFrame()
    sret20 = (px / px.shift(20) - 1.0) * 100.0
    lo = px.rolling(250, min_periods=120).min()
    hi = px.rolling(250, min_periods=120).max()
    spos = (px - lo) / (hi - lo).replace(0.0, np.nan) * 100.0
    return pd.DataFrame({"sret20": sret20, "spos250": spos})


def zigzag(d: pd.DataFrame, pct: float = 0.10) -> list[tuple[int, str, float]]:
    """摆动点标注（仅验证用）：确认反向幅度达到 ``pct`` 才认一个拐点。

    返回 ``[(行号, 'top'|'bottom', 价格), ...]``。真值用 ZigZag 摆动点而不用
    「未来收益」，否则测的是「上涨概率」而不是「识别准确率」。
    """
    h, lo_arr = d["high"].values, d["low"].values
    n = len(d)
    if n < 2:
        return []
    piv: list[tuple[int, str, float]] = []
    trend = None
    ext_i, ext_v = 0, h[0]
    lo_i, lo_v = 0, lo_arr[0]
    for i in range(1, n):
        if trend is None:
            if h[i] > ext_v:
                ext_v, ext_i = h[i], i
            if lo_arr[i] < lo_v:
                lo_v, lo_i = lo_arr[i], i
            if ext_v * (1 - pct) >= lo_v and ext_i < lo_i:
                piv.append((ext_i, "top", ext_v))
                trend = -1
                lo_v, lo_i = lo_arr[i], i
            elif lo_v * (1 + pct) <= ext_v and lo_i < ext_i:
                piv.append((lo_i, "bottom", lo_v))
                trend = 1
                ext_v, ext_i = h[i], i
        elif trend == 1:
            if h[i] > ext_v:
                ext_v, ext_i = h[i], i
            elif lo_arr[i] <= ext_v * (1 - pct):
                piv.append((ext_i, "top", ext_v))
                trend = -1
                lo_v, lo_i = lo_arr[i], i
        elif lo_arr[i] < lo_v:
            lo_v, lo_i = lo_arr[i], i
        elif h[i] >= lo_v * (1 + pct):
            piv.append((lo_i, "bottom", lo_v))
            trend = 1
            ext_v, ext_i = h[i], i
    return piv


def describe(params: dict | None = None) -> dict:
    """把参数翻译成可读的买卖规则（供前端展示，与引擎口径一致）。"""
    p = {**DEFAULT_PARAMS, **(params or {})}
    buy = [
        f"七维滚动 {int(p['win'])} 日分位同时到位：250 日位置、60 日乖离、距 250 日高点跌幅、"
        f"RSI14、RSI6、布林带位置、波动分位，取最小值作为综合分（最短板聚合）",
        f"综合分首次上穿 {int(p['thr'])} 分进入候选；若处于降档环境（板块 20 日跌幅 <−10% "
        f"或板块 250 日位置 <15），阈值上浮至 {int(p['thr']) + 10} 分",
        f"候选后 {int(p['confirm'])} 个交易日内出现收阳且不破前一日低点才买入（右侧确认）",
        "个股 20 日跌幅 >25% 且当日仍创新低时信号挂起，等第一个不破前低的阳线再激活",
    ]
    sell = [
        f"顶部共振 ≥ {int(p['top_cnt'])} 项（七维分位≥{TOP_Q}）时只收紧止损，不作清仓依据"
        "——量析实测顶部信号 20 日后胜率仅 46%、均值仍为正，压的是尾部不是均值",
    ]
    return {
        "buy": buy,
        "sell": sell,
        "note": "本模型只做反转、不做动量：适用于 20 日动量 IC 显著为负的类债核心资产（长江电力等）。",
    }
