"""顶底识别模型事件级验证（移植自量析平台 scripts/tp_validate.py 口径）。

用法::

    uv run stockmaster-tp-validate --codes 600900.SH,600886.SH [--start 2020-01-01] [--thr 75]

口径：
    * 真值 = ZigZag 10% 拐点 + 识别窗口 [-15, +3]（允许提前 15 日识别、滞后 3 日确认）；
    * 信号按**事件**去重，命中判定只看窗口内是否有同向拐点——宽窗口反复计数会把
      精确率虚高到 90%+；
    * 时间外验证按 70/30 切分（早期样本 / 最近 30% 样本）。

结果与量析研究报告（reports/顶底识别模型_研究报告.html）口径对拍：
电力组最短板 ≥75 的头部命中率约 74%（量析实测），明显偏离时须先排查指标口径
（RSI 平滑方式 / BOLL 标准差口径 / 复权方式）再谈参数。
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd


def _load_frames(codes: list[str], start: str | None) -> dict[str, pd.DataFrame]:
    from stockmaster.db import session_scope
    from stockmaster.plugins.turning.services.tp_service import load_frame

    frames: dict[str, pd.DataFrame] = {}
    with session_scope() as session:
        for code in codes:
            frame = load_frame(session, code)
            if frame is None:
                print(f"  [skip] {code}: 数据不足或不存在")
                continue
            if start:
                frame = frame[frame["date"] >= start].reset_index(drop=True)
            if len(frame) >= 400:
                frames[code] = frame
            else:
                print(f"  [skip] {code}: 仅 {len(frame)} 行，需 ≥400")
    return frames


def _label_window(d: pd.DataFrame, piv: list, before: int = 15, after: int = 3) -> pd.Series:
    """把拐点扩成识别窗口：1 = 底部窗口，-1 = 顶部窗口。"""
    lab = pd.Series(0, index=d.index, dtype=int)
    for i, kind, _v in piv:
        lo, hi = max(0, i - before), min(len(d) - 1, i + after)
        val = 1 if kind == "bottom" else -1
        seg = lab.iloc[lo : hi + 1].values
        lab.iloc[lo : hi + 1] = np.where(np.abs(seg) == 1, seg, val)
    return lab


def _event_dedup(mask: np.ndarray, gap: int = 25) -> list[list[int]]:
    idx = np.where(mask)[0]
    if not len(idx):
        return []
    groups, cur = [], [int(idx[0])]
    for i in idx[1:]:
        if i - cur[-1] <= gap:
            cur.append(int(i))
        else:
            groups.append(cur)
            cur = [int(i)]
    groups.append(cur)
    return groups


def _hit_stats(frames: list[pd.DataFrame], col: str, thr: float, target: int) -> dict:
    """事件级命中率 + 事件首日后 20 日前瞻收益。"""
    events = hits = 0
    fwd: list[float] = []
    for f in frames:
        s = f[col].values
        mask = (s >= thr) & ~np.isnan(s)
        for g in _event_dedup(mask):
            i = g[0]
            events += 1
            window = f["lab"].values[i : g[-1] + 1]
            if (window == target).any():
                hits += 1
            if i + 20 < len(f):
                v = f["close"].values[i + 20] / f["close"].values[i] - 1
                fwd.append(float(v))
    rate = hits / events * 100 if events else float("nan")
    mean20 = float(np.mean(fwd)) * 100 if fwd else float("nan")
    return {"events": events, "hit_rate": rate, "mean_fwd20": mean20}


def main() -> int:
    parser = argparse.ArgumentParser(description="顶底识别模型事件级验证")
    parser.add_argument("--codes", required=True, help="逗号分隔的证券代码")
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--thr", type=float, default=75.0, help="底部分位阈值（默认 75）")
    parser.add_argument("--top-cnt", type=int, default=4, help="顶部共振计数阈值（默认 4）")
    args = parser.parse_args()

    from stockmaster.core.tpcore import build, zigzag

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    frames_raw = _load_frames(codes, args.start)
    if not frames_raw:
        print("无可用样本")
        return 1

    frames: list[pd.DataFrame] = []
    for code, d in frames_raw.items():
        out = build(d, {"thr": args.thr, "top_cnt": args.top_cnt, "gate": False})
        out["lab"] = _label_window(d, zigzag(d))
        out["code"] = code
        frames.append(out)
        print(f"  [ok] {code}: {len(d)} 行，ZigZag 拐点 {len(zigzag(d))} 个")

    total_bars = sum(len(f) for f in frames)
    print(f"\n池 {len(frames)} 只 / {total_bars} 行  阈值 thr={args.thr}  top_cnt={args.top_cnt}")

    results: dict[str, dict] = {}
    results["底部·最短板 ≥thr"] = _hit_stats(frames, "tp_bottom", args.thr, 1)
    # 等权平均对照：从七维分位现算
    for f in frames:
        pcols = [f"tp_p_{k}" for k in ("pos250", "bias60", "dd250", "rsi14", "rsi6", "bb_pos", "atr_pct")]
        f["tp_bottom_mean"] = f[pcols].mean(axis=1)
    results["底部·等权平均 ≥90（对照）"] = _hit_stats(frames, "tp_bottom_mean", 90.0, 1)
    results["顶部·共振计数 ≥top_cnt"] = _hit_stats_top(frames, args.top_cnt)

    print(f"\n{'口径':<28}{'事件数':>8}{'命中率':>10}{'20日均值':>12}")
    for name, stat in results.items():
        if stat is None:
            continue
        print(f"{name:<28}{stat['events']:>8}{stat['hit_rate']:>9.0f}%{stat['mean_fwd20']:>11.2f}%")

    # 时间外验证（70/30 切分）
    print("\n时间外验证（底部·最短板 ≥thr）")
    cutoff_dates = []
    for f in frames:
        cutoff_dates.append(f["date"].iloc[int(len(f) * 0.7)])
    cutoff = min(cutoff_dates)
    early = [f[f["date"] < cutoff].reset_index(drop=True) for f in frames]
    late = [f[f["date"] >= cutoff].reset_index(drop=True) for f in frames]
    for name, sub in ((f"  前 70%（<{cutoff}）", early), (f"  后 30%（≥{cutoff}）", late)):
        stat = _hit_stats(sub, "tp_bottom", args.thr, 1)
        print(f"{name:<34}{stat['events']:>8}{stat['hit_rate']:>9.0f}%{stat['mean_fwd20']:>11.2f}%")

    print(
        "\n对拍基准（量析电力组实测）：最短板 ≥75 头部命中率 ~74%、覆盖 ~1.8% 交易日；\n"
        "共振计数显著低于最短板属预期（计数会被连续同向下跌灌满）。"
    )
    return 0


def _hit_stats_top(frames: list[pd.DataFrame], top_cnt: int) -> dict:
    """顶部事件级验证：共振计数首次 ≥top_cnt 的事件，窗口内是否覆盖 ZigZag 高点。"""
    events = hits = 0
    fwd: list[float] = []
    for f in frames:
        cnt = f["tp_top"].values
        first = (cnt >= top_cnt) & (np.r_[np.nan, cnt[:-1]] < top_cnt)
        for g in _event_dedup(first):
            i = g[0]
            events += 1
            window = f["lab"].values[i : g[-1] + 1]
            if (window == -1).any():
                hits += 1
            if i + 20 < len(f):
                fwd.append(float(f["close"].values[i + 20] / f["close"].values[i] - 1))
    rate = hits / events * 100 if events else float("nan")
    mean20 = float(np.mean(fwd)) * 100 if fwd else float("nan")
    return {"events": events, "hit_rate": rate, "mean_fwd20": mean20}


if __name__ == "__main__":
    sys.exit(main())
