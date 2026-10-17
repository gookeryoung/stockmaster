"""算法配置训练：把「算法 + 超参 + 样本」变成一个已注册的模型。

为什么不复用 ``POST /predict/train``
------------------------------------
既有端点是「临时试一次」：算法和 horizon 来自按钮，标的池是默认宇宙。算法配置要
解决的是**可复现**——同样的配置（算法、超参、样本窗口、标的池）应当能重复训练出
可比较的模型，并把参数写进 ``model_registry``。所以这里：

1. 校验超参白名单（未知键直接 422，而不是等 sklearn 抛 TypeError）；
2. 用样本配置解析标的池与窗口，按 ``min_bars`` 剔除历史不足的标的；
3. 调用 ``train_model(..., algo_params=...)``，产物仍是全局模型注册表（模型是
   共享参考数据，不按工作区复制）。

超参白名单按算法分别维护：RandomForest 的 ``max_features`` 这类接受字符串/None 的
参数刻意不开放，避免把「配置校验」变成「替用户猜 sklearn 的合法取值」。
"""

from __future__ import annotations

import json

import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import InsufficientData
from app.db.models import AlgorithmConfig
from app.model.dataset import build_dataset, load_bars_by_symbol
from app.model.models import AVAILABLE_ALGOS
from app.model.train import train_model
from app.schemas import AlgorithmTrainResult, SampleResolveResult
from app.strategy.sample import fetch_sample, resolve_sample
from app.workspace.service import WorkspaceError

#: (类型, 最小值, 最大值)。``int`` 不接受 bool（Python 里 bool 是 int 的子类）。
PARAM_SPECS: dict[str, dict[str, tuple[type, float, float]]] = {
    "hist_gbdt": {
        "max_iter": (int, 10, 5000),
        "learning_rate": (float, 0.0001, 1.0),
        "max_depth": (int, 1, 32),
        "min_samples_leaf": (int, 1, 10000),
        "l2_regularization": (float, 0.0, 100.0),
    },
    "random_forest": {
        "n_estimators": (int, 10, 2000),
        "max_depth": (int, 1, 64),
        "min_samples_leaf": (int, 1, 10000),
    },
    "logistic": {
        "C": (float, 0.000001, 1000.0),
        "max_iter": (int, 100, 20000),
    },
}


def invalid_params(detail: str) -> WorkspaceError:
    return WorkspaceError(detail, status_code=422, code="invalid_algo_params")


def validate_algo_params(algo: str, params: dict) -> dict:
    """校验并归一化超参；未知键/越界/类型错误一律 422。"""
    if algo not in AVAILABLE_ALGOS:
        raise invalid_params(f"未知算法 {algo!r}；可选：{', '.join(AVAILABLE_ALGOS)}")
    spec = PARAM_SPECS[algo]
    cleaned: dict[str, float | int] = {}
    for key, raw in (params or {}).items():
        if key not in spec:
            raise invalid_params(f"算法 {algo} 不支持超参 {key!r}；可用：{', '.join(spec)}")
        expected, low, high = spec[key]
        if isinstance(raw, bool):
            raise invalid_params(f"超参 {key} 需要数值，收到布尔值")
        if expected is int:
            if not isinstance(raw, int):
                raise invalid_params(f"超参 {key} 需要整数")
            value: float | int = raw
        else:
            if not isinstance(raw, int | float):
                raise invalid_params(f"超参 {key} 需要数字")
            value = float(raw)
        if not (low <= float(value) <= high):
            raise invalid_params(f"超参 {key} 应在 [{low}, {high}] 内，收到 {value}")
        cleaned[key] = value
    return cleaned


def _resolve_training_sample(session: Session, config: AlgorithmConfig) -> tuple[SampleResolveResult, int]:
    """解析训练样本，同时给出「每个标的至少多少根日线」的门槛。"""
    if config.sample_id is not None:
        sample = fetch_sample(session, config.sample_id, config.workspace_id)
        if sample is None:
            raise WorkspaceError(
                "算法配置绑定的样本配置不存在（可能已被删除），请重新选择样本",
                status_code=422,
                code="sample_not_found",
            )
        return resolve_sample(session, sample), sample.min_bars

    # 未绑定样本：沿用调度器的默认宇宙与全量历史，horizon 取算法配置自己的值。
    from app.jobs.tasks import _default_universe

    workspace_id = config.workspace_id if settings.data_isolation else None
    resolved = SampleResolveResult(codes=_default_universe(workspace_id=workspace_id), horizon=config.horizon)
    return resolved, 60


def _filter_frames(
    frames: dict[str, pd.DataFrame],
    *,
    start,
    end,
    min_bars: int,
) -> dict[str, pd.DataFrame]:
    """按样本窗口裁剪，并剔除历史长度不足 ``min_bars`` 的标的。"""
    filtered: dict[str, pd.DataFrame] = {}
    for code, frame in frames.items():
        if frame is None or frame.empty:
            continue
        if start is not None:
            frame = frame[frame["trade_date"] >= start]
        if end is not None:
            frame = frame[frame["trade_date"] <= end]
        if len(frame) >= min_bars:
            filtered[code] = frame.reset_index(drop=True)
    return filtered


def train_algorithm(session: Session, config: AlgorithmConfig) -> AlgorithmTrainResult:
    """按算法配置训练一个模型并返回其指标。"""
    params = validate_algo_params(config.algo, json.loads(config.params_json or "{}"))
    resolved, min_bars = _resolve_training_sample(session, config)
    if not resolved.codes:
        raise WorkspaceError(
            "样本没有任何标的（标的池为空）；请检查样本配置或先同步行情",
            status_code=409,
            code="empty_sample",
        )

    frames = load_bars_by_symbol(session, resolved.codes, adjust="qfq")
    frames = _filter_frames(frames, start=resolved.start, end=resolved.end, min_bars=min_bars)
    if not frames:
        raise WorkspaceError(
            f"样本窗口内没有足够的历史数据（每个标的至少需要 {min_bars} 根日线）；请扩大窗口或先同步更多历史",
            status_code=409,
            code="insufficient_data",
        )

    dataset = build_dataset(frames, horizon=config.horizon)
    try:
        model = train_model(
            dataset,
            algo=config.algo,
            test_fraction=config.test_fraction,
            algo_params=params,
            persist=True,
        )
    except InsufficientData as exc:
        raise WorkspaceError(str(exc), status_code=409, code="insufficient_data") from exc

    return AlgorithmTrainResult(
        model_id=model.model_id,
        algo=model.algo,
        horizon=model.horizon,
        metrics=model.metrics,
        n_train=model.n_train,
        n_test=model.n_test,
    )


__all__ = ["PARAM_SPECS", "train_algorithm", "validate_algo_params"]
