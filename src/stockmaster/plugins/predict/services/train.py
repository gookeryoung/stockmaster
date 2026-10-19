"""Training orchestration and model persistence."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import joblib

from stockmaster.config import settings
from stockmaster.core.errors import InsufficientData, ModelNotTrained
from stockmaster.core.logging import get_logger
from stockmaster.db.models import ModelRegistry, Prediction
from stockmaster.db.session import session_scope
from stockmaster.plugins.predict.services.dataset import (
    Dataset,
    Split,
    build_dataset,
    build_symbol_features,
    chronological_split,
    load_bars_by_symbol,
    time_series_folds,
)
from stockmaster.plugins.predict.services.evaluate import evaluate_predictions
from stockmaster.plugins.predict.services.models import (
    AVAILABLE_ALGOS,
    TrainedModel,
    extract_importance,
    fit_model,
    predict_proba,
)

log = get_logger(__name__)

__all__ = ["load_active_model", "score_symbols", "train_all_horizons", "train_model"]


def _model_id(algo: str, horizon: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{algo}-h{horizon}-{stamp}-{uuid.uuid4().hex[:6]}"


def train_model(
    dataset: Dataset,
    *,
    algo: str = "hist_gbdt",
    test_fraction: float | None = None,
    validation_fraction: float = 0.0,
    persist: bool = True,
    walk_forward: bool = False,
    algo_params: dict | None = None,
) -> TrainedModel:
    """Fit, evaluate out-of-sample, and optionally persist a model.

    The test partition is never touched during fitting, and the training tail is
    purged of rows whose labels overlap it (see
    :func:`app.model.dataset.chronological_split`).

    ``algo_params`` overrides the estimator's defaults (validated by the caller),
    and is recorded in ``model_registry.params_json`` so a configured model can be
    reproduced.
    """
    if algo not in AVAILABLE_ALGOS:
        raise ValueError(f"unknown algo {algo!r}; expected {AVAILABLE_ALGOS}")
    if dataset.frame.empty:
        raise InsufficientData("dataset is empty; sync history before training")
    overrides = dict(algo_params or {})

    test_fraction = settings.test_fraction if test_fraction is None else test_fraction
    split: Split = chronological_split(dataset, test_fraction=test_fraction, validation_fraction=validation_fraction)

    if split.train.empty or split.test.empty:
        raise InsufficientData(
            "not enough history to form train/test partitions "
            f"({len(split.train)} train rows, {len(split.test)} test rows)"
        )

    estimator = fit_model(
        algo,
        split.train,
        dataset.feature_columns,
        label_column=dataset.label_column,
        **overrides,
    )
    probabilities = predict_proba(estimator, split.test, dataset.feature_columns)
    metrics = evaluate_predictions(
        split.test,
        probabilities,
        return_column=dataset.forward_return_column,
        label_column=dataset.label_column,
    )
    metrics["split"] = split.describe()
    metrics["dataset"] = dataset.describe()
    metrics["purged_rows"] = split.purged_rows

    # Validation slice, when configured, is reported separately so a user can see
    # overfitting between partitions rather than only a single test number.
    if not split.validation.empty:
        validation_probabilities = predict_proba(estimator, split.validation, dataset.feature_columns)
        validation_metrics = evaluate_predictions(
            split.validation,
            validation_probabilities,
            return_column=dataset.forward_return_column,
            label_column=dataset.label_column,
        )
        metrics["validation"] = {
            "n": validation_metrics.get("n"),
            "auc": validation_metrics.get("auc"),
            "ic_spearman": validation_metrics.get("ic_spearman"),
            "accuracy": validation_metrics.get("accuracy"),
        }

    if walk_forward:
        metrics["walk_forward"] = _walk_forward_report(dataset, algo, overrides)

    importance = extract_importance(estimator, dataset.feature_columns)

    model = TrainedModel(
        model_id=_model_id(algo, dataset.horizon),
        algo=algo,
        horizon=dataset.horizon,
        estimator=estimator,
        feature_columns=list(dataset.feature_columns),
        metrics=metrics,
        importance=importance,
        train_span=_span(split.train),
        test_span=_span(split.test),
        n_train=len(split.train),
        n_test=len(split.test),
        params=overrides,
    )

    log.info(
        "trained %s horizon=%d train=%d test=%d AUC=%s IC=%s",
        algo,
        dataset.horizon,
        len(split.train),
        len(split.test),
        _fmt(metrics.get("auc")),
        _fmt(metrics.get("ic_spearman")),
    )

    if persist:
        _persist(model)
    return model


def _span(frame) -> tuple | None:
    if frame is None or frame.empty:
        return None
    return (str(frame["trade_date"].min()), str(frame["trade_date"].max()))


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _walk_forward_report(dataset: Dataset, algo: str, overrides: dict | None = None) -> list[dict]:
    """Walk-forward stability report across expanding windows."""
    folds = time_series_folds(dataset.frame, n_splits=5, horizon=dataset.horizon)
    report: list[dict] = []
    for index, (train, test) in enumerate(folds):
        try:
            estimator = fit_model(
                algo,
                train,
                dataset.feature_columns,
                label_column=dataset.label_column,
                **(overrides or {}),
            )
            probabilities = predict_proba(estimator, test, dataset.feature_columns)
            metrics = evaluate_predictions(
                test,
                probabilities,
                return_column=dataset.forward_return_column,
                label_column=dataset.label_column,
            )
        except Exception as exc:
            report.append({"fold": index, "error": str(exc)[:200]})
            continue
        report.append(
            {
                "fold": index,
                "n_train": len(train),
                "n_test": len(test),
                "test_start": str(test["trade_date"].min()),
                "test_end": str(test["trade_date"].max()),
                "auc": metrics.get("auc"),
                "ic_spearman": metrics.get("ic_spearman"),
                "accuracy": metrics.get("accuracy"),
            }
        )
    return report


def _persist(model: TrainedModel) -> Path:
    """Write the artefact to disk and register it, deactivating older models."""
    settings.ensure_dirs()
    artifact = settings.models_dir / f"{model.model_id}.joblib"
    joblib.dump(
        {
            "estimator": model.estimator,
            "feature_columns": model.feature_columns,
            "algo": model.algo,
            "horizon": model.horizon,
        },
        artifact,
    )

    with session_scope() as session:
        # Only one active model per horizon, so scoring is deterministic.
        for row in session.query(ModelRegistry).filter_by(horizon=model.horizon, is_active=True):
            row.is_active = False
        session.merge(
            ModelRegistry(
                model_id=model.model_id,
                algo=model.algo,
                horizon=model.horizon,
                params_json=json.dumps({"algo": model.algo, "params": model.params}, default=str),
                features_json=json.dumps(model.feature_columns),
                train_start=_date_or_none(model.train_span, 0),
                train_end=_date_or_none(model.train_span, 1),
                test_start=_date_or_none(model.test_span, 0),
                test_end=_date_or_none(model.test_span, 1),
                metrics_json=json.dumps(model.metrics, default=str),
                importance_json=json.dumps(model.importance),
                n_train=model.n_train,
                n_test=model.n_test,
                artifact_path=str(artifact),
                is_active=True,
                trained_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    return artifact


def _date_or_none(span: tuple | None, index: int):
    if not span:
        return None
    from datetime import date as _date

    return _date.fromisoformat(str(span[index]))


def load_active_model(horizon: int = 1) -> TrainedModel:
    """Load the active model for a horizon from disk."""
    with session_scope() as session:
        row = (
            session.query(ModelRegistry)
            .filter_by(horizon=horizon, is_active=True)
            .order_by(ModelRegistry.trained_at.desc())
            .first()
        )
        if row is None:
            raise ModelNotTrained(f"no active model for horizon={horizon}; train one first")
        payload = {
            "model_id": row.model_id,
            "algo": row.algo,
            "horizon": row.horizon,
            "artifact_path": row.artifact_path,
            "metrics_json": row.metrics_json,
            "importance_json": row.importance_json,
            "params_json": row.params_json,
            "n_train": row.n_train,
            "n_test": row.n_test,
            "train_start": row.train_start,
            "train_end": row.train_end,
            "test_start": row.test_start,
            "test_end": row.test_end,
        }

    path = Path(payload["artifact_path"])
    if not path.exists():
        raise ModelNotTrained(f"model artifact missing: {path}")

    bundle = joblib.load(path)
    return TrainedModel(
        model_id=payload["model_id"],
        algo=payload["algo"],
        horizon=payload["horizon"],
        estimator=bundle["estimator"],
        feature_columns=bundle["feature_columns"],
        metrics=json.loads(payload["metrics_json"] or "{}"),
        importance=json.loads(payload["importance_json"] or "{}"),
        n_train=payload["n_train"],
        n_test=payload["n_test"],
        train_span=((str(payload["train_start"]), str(payload["train_end"])) if payload["train_start"] else None),
        test_span=((str(payload["test_start"]), str(payload["test_end"])) if payload["test_start"] else None),
        params=json.loads(payload["params_json"] or "{}").get("params", {}),
    )


def train_all_horizons(
    bars_by_symbol: dict,
    *,
    horizons: list[int] | None = None,
    algo: str = "hist_gbdt",
    persist: bool = True,
) -> dict[int, TrainedModel]:
    """Train one model per horizon."""
    horizons = horizons or settings.horizons
    models: dict[int, TrainedModel] = {}
    for horizon in horizons:
        dataset = build_dataset(bars_by_symbol, horizon=horizon)
        if dataset.frame.empty:
            log.warning("skipping horizon=%d: empty dataset", horizon)
            continue
        models[horizon] = train_model(dataset, algo=algo, persist=persist)
    if not models:
        raise InsufficientData("no horizon produced a trainable dataset")
    return models


def score_symbols(
    model: TrainedModel,
    bars_by_symbol: dict,
    *,
    persist: bool = True,
) -> list[dict]:
    """Score the most recent bar of each symbol with a trained model.

    Only the final row per symbol is scored: that is the actionable, point-in-time
    prediction. Returns one record per symbol that had enough history.
    """
    records: list[dict] = []
    for code, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue
        features = build_symbol_features(bars)
        if features.empty:
            continue
        # The last row is the live one; drop it if features are too sparse.
        last = features.iloc[[-1]].copy()
        present = last[model.feature_columns].notna().sum(axis=1).iloc[0]
        if present < max(1, len(model.feature_columns) // 3):
            continue
        try:
            probability = float(model.predict_proba(last)[0])
        except Exception as exc:
            log.debug("scoring failed for %s: %s", code, exc)
            continue

        trade_date = last["trade_date"].iloc[0]
        records.append(
            {
                "model_id": model.model_id,
                "code": code,
                "trade_date": trade_date,
                "horizon": model.horizon,
                "prob_up": probability,
                "exp_ret": None,
                "interval_lo": None,
                "interval_hi": None,
                "features_json": json.dumps(
                    {c: (None if last[c].isna().iloc[0] else float(last[c].iloc[0])) for c in model.feature_columns}
                ),
            }
        )

    if persist and records:
        _persist_predictions(records)
    return records


def _persist_predictions(records: list[dict]) -> None:
    """Upsert predictions so re-scoring the same day is idempotent."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    with session_scope() as session:
        stmt = sqlite_insert(Prediction).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                Prediction.model_id,
                Prediction.code,
                Prediction.trade_date,
                Prediction.horizon,
            ],
            set_={
                "prob_up": stmt.excluded.prob_up,
                "features_json": stmt.excluded.features_json,
            },
        )
        session.execute(stmt)


def load_dataset_from_db(codes: list[str], *, horizon: int = 1, adjust: str = "qfq") -> Dataset:
    """Convenience helper: read bars from the database and build a dataset."""
    with session_scope() as session:
        bars = load_bars_by_symbol(session, codes, adjust=adjust)
    return build_dataset(bars, horizon=horizon)
