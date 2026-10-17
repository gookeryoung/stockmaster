"""Prediction layer: dataset construction, models, evaluation and training."""

from stockmaster.model.dataset import (
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
from stockmaster.model.evaluate import (
    classification_metrics,
    decile_spread,
    evaluate_predictions,
    information_coefficient,
    quantile_returns,
)
from stockmaster.model.models import (
    AVAILABLE_ALGOS,
    TrainedModel,
    build_estimator,
    extract_importance,
    fit_model,
    predict_proba,
)
from stockmaster.model.train import (
    load_active_model,
    load_dataset_from_db,
    score_symbols,
    train_all_horizons,
    train_model,
)

__all__ = [
    "AVAILABLE_ALGOS",
    "FEATURE_COLUMNS",
    "Dataset",
    "Split",
    "TrainedModel",
    "add_labels",
    "build_dataset",
    "build_estimator",
    "build_symbol_features",
    "chronological_split",
    "classification_metrics",
    "decile_spread",
    "evaluate_predictions",
    "extract_importance",
    "fit_model",
    "information_coefficient",
    "load_active_model",
    "load_bars_by_symbol",
    "load_dataset_from_db",
    "predict_proba",
    "quantile_returns",
    "score_symbols",
    "time_series_folds",
    "train_all_horizons",
    "train_model",
]
