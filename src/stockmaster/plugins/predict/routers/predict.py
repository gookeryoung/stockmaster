"""Prediction endpoints: model cards, training and scoring."""

from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmaster.core.deps import canonical_code, db_session
from stockmaster.core.errors import InsufficientData, InvalidAlgoParams, ModelNotTrained
from stockmaster.core.logging import get_logger
from stockmaster.db.models import Instrument, ModelRegistry, Prediction
from stockmaster.db.session import session_scope
from stockmaster.plugins.predict.services.dataset import (
    FEATURE_COLUMNS,
    MIN_FEATURES_PRESENT,
    load_bars_by_symbol,
)
from stockmaster.plugins.predict.services.models import validate_algo_params
from stockmaster.plugins.predict.services.train import load_active_model, score_symbols
from stockmaster.schemas import (
    ModelCard,
    PredictionListResponse,
    PredictionOut,
    TrainRequest,
    TrainResponse,
)

log = get_logger(__name__)
router = APIRouter(tags=["prediction"])

DISCLAIMER = (
    "预测基于历史数据与统计模型，样本外表现不代表未来收益。模型指标（AUC/IC）已包含随机基线对照，请勿据此直接交易。"
)


def _card(row: ModelRegistry) -> ModelCard:
    return ModelCard(
        model_id=row.model_id,
        algo=row.algo,
        horizon=row.horizon,
        is_active=row.is_active,
        trained_at=row.trained_at,
        n_train=row.n_train,
        n_test=row.n_test,
        train_start=row.train_start,
        train_end=row.train_end,
        test_start=row.test_start,
        test_end=row.test_end,
        metrics=json.loads(row.metrics_json or "{}"),
        importance=json.loads(row.importance_json or "{}"),
    )


@router.get("/predict/models", response_model=list[ModelCard])
def list_models(
    horizon: int | None = Query(default=None, ge=1, le=60),
    session: Session = Depends(db_session),
):
    """Trained model cards, newest first."""
    stmt = select(ModelRegistry).order_by(ModelRegistry.trained_at.desc())
    if horizon is not None:
        stmt = stmt.where(ModelRegistry.horizon == horizon)
    return [_card(row) for row in session.execute(stmt).scalars()]


def _resolve_features(requested: list[str] | None) -> tuple[tuple[str, ...], int]:
    """把页面选择的特征子集规范成 ``build_dataset`` 的入参。

    未指定（``None``）时用全部特征，保持既有默认行为；显式空集合或含未知特征名
    直接 422，而不是悄悄用一个被裁剪过的输入训练出一个无法解释的模型。
    """
    if requested is None:
        return FEATURE_COLUMNS, MIN_FEATURES_PRESENT

    unknown = sorted({name for name in requested if name not in FEATURE_COLUMNS})
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown features: {', '.join(unknown)}",
        )
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="select at least one feature",
        )

    chosen = set(requested)
    # 按 FEATURE_COLUMNS 的规范顺序去重，保证同一选择可复现。
    selected = tuple(name for name in FEATURE_COLUMNS if name in chosen)
    # 子集不可能满足全量缺失门槛，按比例收紧，否则所有行都会因「缺失过多」被丢弃。
    min_present = max(1, min(MIN_FEATURES_PRESENT, round(len(selected) / 2)))
    return selected, min_present


@router.post("/predict/train", response_model=TrainResponse)
def train(payload: TrainRequest, session: Session = Depends(db_session)):
    """Train a model on the stored universe.

    Runs synchronously because the default universe is small enough (a few
    hundred symbols) to finish in seconds; a full-market train belongs in the
    scheduler rather than an HTTP request.

    预测页会按请求即时自定义流水线（特征子集、算法超参、测试集占比），这里统一
    校验并透传；三者都不传时行为与从前完全一致。
    """
    from stockmaster.jobs.tasks import _default_universe

    codes = payload.codes or _default_universe()
    if not codes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no universe available; sync instruments and daily bars first",
        )

    bars = load_bars_by_symbol(session, codes, adjust="qfq")
    if not bars:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no stored daily bars for the requested universe",
        )

    feature_columns, min_features_present = _resolve_features(payload.features)
    # 算法超参白名单：未知键/越界/类型错误在训练前就以 422 拒绝。
    try:
        algo_params = validate_algo_params(payload.algo, payload.algo_params)
    except InvalidAlgoParams as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    try:
        if payload.horizon is not None:
            from stockmaster.plugins.predict.services.dataset import build_dataset
            from stockmaster.plugins.predict.services.train import train_model

            dataset = build_dataset(
                bars,
                horizon=payload.horizon,
                feature_columns=feature_columns,
                min_features_present=min_features_present,
            )
            model = train_model(
                dataset,
                algo=payload.algo,
                test_fraction=payload.test_fraction,
                algo_params=algo_params,
                persist=True,
            )
            return TrainResponse(
                model_id=model.model_id,
                horizon=payload.horizon,
                metrics=model.metrics,
                n_train=model.n_train,
                n_test=model.n_test,
            )

        from stockmaster.plugins.predict.services.train import train_all_horizons

        models = train_all_horizons(
            bars,
            algo=payload.algo,
            feature_columns=feature_columns,
            min_features_present=min_features_present,
            test_fraction=payload.test_fraction,
            algo_params=algo_params,
            persist=True,
        )
    except InsufficientData as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return TrainResponse(
        models={
            str(h): {
                "model_id": m.model_id,
                "auc": m.metrics.get("auc"),
                "ic": m.metrics.get("ic_spearman"),
            }
            for h, m in models.items()
        }
    )


@router.get("/predict", response_model=PredictionListResponse)
def list_predictions(
    horizon: int = Query(default=1, ge=1, le=60),
    trade_date: date | None = None,
    min_prob: float | None = Query(default=None, ge=0.0, le=1.0),
    limit: int = Query(default=100, ge=1, le=2000),
    session: Session = Depends(db_session),
):
    """Stored predictions for a date, highest probability first."""
    target_date = trade_date
    if target_date is None:
        target_date = session.execute(
            select(Prediction.trade_date)
            .where(Prediction.horizon == horizon)
            .order_by(Prediction.trade_date.desc())
            .limit(1)
        ).scalar()

    if target_date is None:
        return PredictionListResponse(trade_date=None, horizon=horizon, items=[])

    stmt = (
        select(Prediction)
        .where(Prediction.trade_date == target_date, Prediction.horizon == horizon)
        .order_by(Prediction.prob_up.desc().nullslast())
        .limit(limit)
    )
    if min_prob is not None:
        stmt = stmt.where(Prediction.prob_up >= min_prob)

    rows = list(session.execute(stmt).scalars())
    names = (
        {
            i.code: i.name
            for i in session.execute(select(Instrument).where(Instrument.code.in_([r.code for r in rows]))).scalars()
        }
        if rows
        else {}
    )

    return PredictionListResponse(
        trade_date=target_date,
        horizon=horizon,
        items=[
            PredictionOut(
                code=r.code,
                name=names.get(r.code, ""),
                trade_date=r.trade_date,
                horizon=r.horizon,
                prob_up=r.prob_up,
                model_id=r.model_id,
            )
            for r in rows
        ],
    )


@router.get("/stocks/{code}/prediction")
def prediction_detail(
    code: str,
    horizon: int = Query(default=1, ge=1, le=60),
    session: Session = Depends(db_session),
):
    """Prediction detail for one symbol, including a feature/importance breakdown."""
    canon = canonical_code(code)
    row = session.execute(
        select(Prediction)
        .where(Prediction.code == canon, Prediction.horizon == horizon)
        .order_by(Prediction.trade_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no prediction stored for {canon} at horizon {horizon}",
        )

    model_row = session.execute(
        select(ModelRegistry).where(ModelRegistry.model_id == row.model_id)
    ).scalar_one_or_none()

    features = json.loads(row.features_json or "{}")
    importance = json.loads(model_row.importance_json or "{}") if model_row else {}
    ranked = sorted(
        (
            {"feature": name, "value": features.get(name), "importance": weight}
            for name, weight in importance.items()
            if name in features
        ),
        key=lambda item: -abs(item["importance"]),
    )[:10]

    return {
        "code": canon,
        "trade_date": row.trade_date,
        "horizon": horizon,
        "prob_up": row.prob_up,
        "exp_ret": row.exp_ret,
        "interval_lo": row.interval_lo,
        "interval_hi": row.interval_hi,
        "model_id": row.model_id,
        "features": features,
        "top_features": ranked,
        "model_metrics": json.loads(model_row.metrics_json or "{}") if model_row else {},
        "disclaimer": DISCLAIMER,
    }


@router.post("/predict/run")
def run_prediction(horizon: int = Query(default=1, ge=1, le=60)):
    """Score the current universe with the active model."""
    from stockmaster.jobs.tasks import _default_universe

    try:
        model = load_active_model(horizon=horizon)
    except ModelNotTrained as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{exc}; POST /predict/train first",
        ) from exc

    codes = _default_universe()
    with session_scope() as session:
        bars = load_bars_by_symbol(session, codes, adjust="qfq")
    records = score_symbols(model, bars, persist=True)
    return {
        "model_id": model.model_id,
        "horizon": horizon,
        "scored": len(records),
        "disclaimer": DISCLAIMER,
    }
