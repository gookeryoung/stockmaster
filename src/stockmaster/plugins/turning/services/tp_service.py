"""turning 插件服务层：顶底识别信号的计算与事件提取。

服务层不依赖 FastAPI，CLI 与 jobs 可复用。数据从 ``DailyBar`` 读取（默认前复权，
缺失时回落不复权），特征与信号计算全部委托 :mod:`stockmaster.core.tpcore`。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from stockmaster.core.tpcore import DEFAULT_PARAMS, build, sector_index

#: 单次扫描/取历史的标的数量上限
MAX_CODES = 50

PERCENTILE_FIELDS = ("pos250", "bias60", "dd250", "rsi14", "rsi6", "bb_pos", "atr_pct")


def load_frame(session, code: str, adjust: str = "qfq") -> pd.DataFrame | None:
    """读取一只标的的完整日线序列为 tpcore 输入帧（date/open/high/low/close/volume）。"""
    from stockmaster.sync import repository as repo

    def _to_frame(adjust_key: str) -> pd.DataFrame | None:
        bars = repo.get_bars(session, code, adjust_key)
        if len(bars) < 60:
            return None
        return pd.DataFrame(
            {
                "date": [str(b.trade_date) for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            }
        )

    frame = _to_frame(adjust)
    if frame is None and adjust != "raw":
        frame = _to_frame("raw")
    return frame


def scan_codes(
    session,
    codes: list[str],
    params: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """对一组标的计算最新顶底状态。

    返回 ``(items, missing)``：``items`` 按请求顺序排列，数据不足或缺失的代码
    进入 ``missing``。板块闸门用请求池内的等权指数（池太小则闸门自动退化为
    只看个股创新低挂起）。
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    frames: dict[str, pd.DataFrame] = {}
    for code in codes:
        frame = load_frame(session, code)
        if frame is not None:
            frames[code] = frame

    sec = sector_index(codes, frames) if len(frames) >= 3 else None
    items: list[dict] = []
    for code in codes:
        frame = frames.get(code)
        if frame is None:
            continue
        result = build(frame, p, sec)
        row = result.iloc[-1]
        items.append(
            {
                "code": code,
                "trade_date": str(row.get("date", "")),
                "close": float(row["close"]),
                "bottom": None if pd.isna(row["tp_bottom"]) else round(float(row["tp_bottom"]), 1),
                "top_count": None if pd.isna(row["tp_top"]) else int(row["tp_top"]),
                "gate": int(row["tp_gate"]),
                "hold_thr": None if pd.isna(row["tp_hold_thr"]) else float(row["tp_hold_thr"]),
                "candidate": bool(row["tp_cand"]),
                "entry": bool(row["tp_entry"]),
                "percentiles": {
                    key: (None if pd.isna(row[f"tp_p_{key}"]) else round(float(row[f"tp_p_{key}"]), 1))
                    for key in PERCENTILE_FIELDS
                },
            }
        )
    missing = [code for code in codes if code not in frames]
    return items, missing


def history_events(
    session,
    code: str,
    kind: str = "bottom",
    params: dict | None = None,
    limit: int = 50,
) -> list[dict]:
    """提取一只标的的历史顶底事件（按时间升序，取最近 ``limit`` 个）。

    ``kind="bottom"`` 返回冷却去重后的候选及其右侧确认结果；
    ``kind="top"`` 返回顶部共振计数首次达到阈值的事件。
    """
    if kind not in ("bottom", "top"):
        raise ValueError("kind must be 'bottom' or 'top'")
    p = {**DEFAULT_PARAMS, **(params or {})}
    frame = load_frame(session, code)
    if frame is None:
        return []
    result = build(frame, p, None)
    if kind == "bottom":
        mask = result["tp_cand"].fillna(False).astype(bool)
        scores = result["tp_bottom"]
        entries = result["tp_entry"]
    else:
        cnt = result["tp_top"]
        first = (cnt >= int(p["top_cnt"])) & (cnt.shift(1) < int(p["top_cnt"]))
        mask = first.fillna(False).astype(bool)
        scores = cnt
        entries = pd.Series(False, index=result.index)
    idx = np.where(mask.values)[0]
    events: list[dict] = []
    for i in idx[-limit:]:
        row = result.iloc[i]
        events.append(
            {
                "trade_date": str(row.get("date", "")),
                "kind": kind,
                "close": float(row["close"]),
                "score": round(float(scores.iloc[i]), 1) if not pd.isna(scores.iloc[i]) else 0.0,
                "gate": int(row["tp_gate"]),
                "entry": bool(entries.iloc[i]),
            }
        )
    return events


def latest_trade_date(items: list[dict]) -> str | None:
    """扫描结果中最大的交易日（用于响应头字段）。"""
    days = [item["trade_date"] for item in items if item.get("trade_date")]
    return max(days) if days else None


def validate_codes(raw: str) -> list[str]:
    """解析并规范化逗号分隔的代码列表。"""
    from stockmaster.core.errors import DataError
    from stockmaster.core.symbols import normalize

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise ValueError("codes 不能为空")
    codes: list[str] = []
    seen: set[str] = set()
    for part in parts:
        try:
            canon = normalize(part)
        except DataError as exc:
            raise ValueError(f"invalid code: {part}") from exc
        if canon not in seen:
            seen.add(canon)
            codes.append(canon)
    if len(codes) > MAX_CODES:
        raise ValueError(f"最多支持 {MAX_CODES} 个代码")
    return codes


def as_of_ok(trade_date: str | None, as_of: date | None) -> bool:
    """事件是否不晚于 as_of（供未来按需过滤）。"""
    if as_of is None or not trade_date:
        return True
    return str(trade_date) <= as_of.isoformat()
