"""Prediction layer: dataset construction, models, evaluation and training."""

from app.model.dataset import (
    FEATURE_COLUMNS,
    Dataset,
    Split,
    add_labels,
    build_dataset,
    build_symbol_features,
    chronological_split,
    load_bars_by_symbol,
    time_series_folds,
)
from app.model.evaluate import (
    classification_metrics,
    decile_spread,
    evaluate_predictions,
    information_coefficient,
    quantile_returns,
)
from app.model.models import (
    AVAILABLE_ALGOS,
    TrainedModel,
    build_estimator,
    extract_importance,
    fit_model,
    predict_proba,
)
from app.model.train import (
    load_active_model,
    load_dataset_from_db,
    score_symbols,
    train_all_horizons,
    train_model,
)

__all__ = [
    "FEATURE_COLUMNS",
    "Dataset",
    "Split",
    "add_labels",
    "build_dataset",
    "build_symbol_features",
    "chronological_split",
    "load_bars_by_symbol",
    "time_series_folds",
    "classification_metrics",
    "decile_spread",
    "evaluate_predictions",
    "information_coefficient",
    "quantile_returns",
    "AVAILABLE_ALGOS",
    "TrainedModel",
    "build_estimator",
    "extract_importance",
    "fit_model",
    "predict_proba",
    "load_active_model",
    "load_dataset_from_db",
    "score_symbols",
    "train_all_horizons",
    "train_model",
]
