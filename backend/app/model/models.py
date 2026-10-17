"""Model definitions.

Scope: gradient boosting and random forests for tabular, point-in-time features.

Deliberate choices
------------------
* ``HistGradientBoostingClassifier`` and ``RandomForestClassifier`` only. No
  LightGBM/XGBoost: they add native-build risk on Windows for a marginal gain
  here, and the interface below makes them droppable in later.
* Class weights are balanced by default because daily up/down labels are close
  to 50/50 but drift in trending regimes, and an unweighted model silently
  biases toward the majority class.
* Every estimator is seeded from settings so a run is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from app.config import settings
from app.core.errors import ModelNotTrained
from app.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["AVAILABLE_ALGOS", "TrainedModel", "build_estimator", "fit_model", "predict_proba"]

AVAILABLE_ALGOS = ("hist_gbdt", "random_forest", "logistic")


class Estimator(Protocol):
    def fit(self, X, y): ...
    def predict_proba(self, X): ...
    def predict(self, X): ...


@dataclass
class TrainedModel:
    """A fitted estimator plus everything needed to reproduce and explain it."""

    model_id: str
    algo: str
    horizon: int
    estimator: Any
    feature_columns: list[str]
    metrics: dict
    importance: dict
    train_span: tuple | None = None
    test_span: tuple | None = None
    n_train: int = 0
    n_test: int = 0
    #: 实际生效的超参（算法配置训练时由调用方传入），写进 model_registry 以便复现。
    params: dict = field(default_factory=dict)

    def predict_proba(self, feature_frame: pd.DataFrame) -> np.ndarray:
        """Positive-class probability for each row."""
        missing = [c for c in self.feature_columns if c not in feature_frame.columns]
        if missing:
            raise ModelNotTrained(f"feature frame missing columns required by {self.model_id}: {missing}")
        matrix = feature_frame[self.feature_columns].astype(float).to_numpy()
        probabilities = self.estimator.predict_proba(matrix)
        # predict_proba returns columns ordered by classes_; find the 1 column.
        classes = list(getattr(_final_estimator(self.estimator), "classes_", [0, 1]))
        if 1 in classes:
            return probabilities[:, classes.index(1)]
        return probabilities[:, -1]


def build_estimator(algo: str, *, seed: int | None = None, **overrides) -> Estimator:
    """Instantiate an estimator by name.

    Hyperparameters are intentionally modest and regularised; the goal is a
    trustworthy baseline, not a leaderboard score.
    """
    seed = settings.random_seed if seed is None else seed

    if algo == "hist_gbdt":
        from sklearn.ensemble import HistGradientBoostingClassifier

        params = {
            "max_iter": 300,
            "learning_rate": 0.05,
            "max_depth": 4,
            "min_samples_leaf": 50,
            "l2_regularization": 1.0,
            "early_stopping": False,
            "random_state": seed,
        }
        params.update(overrides)
        return HistGradientBoostingClassifier(**params)

    if algo == "random_forest":
        from sklearn.ensemble import RandomForestClassifier

        params = {
            "n_estimators": 300,
            "max_depth": 8,
            "min_samples_leaf": 50,
            "max_features": "sqrt",
            "class_weight": "balanced_subsample",
            "n_jobs": -1,
            "random_state": seed,
        }
        params.update(overrides)
        return RandomForestClassifier(**params)

    if algo == "logistic":
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        params = {
            "C": 0.5,
            "max_iter": 2000,
            "class_weight": "balanced",
            "random_state": seed,
        }
        params.update(overrides)
        return Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
                ("clf", LogisticRegression(**params)),
            ]
        )

    raise ValueError(f"unknown algo {algo!r}; expected one of {', '.join(AVAILABLE_ALGOS)}")


def fit_model(
    algo: str,
    train: pd.DataFrame,
    feature_columns: list[str],
    *,
    label_column: str = "label",
    seed: int | None = None,
    **overrides,
) -> Estimator:
    """Fit an estimator on a training frame.

    Rows with a missing label are dropped. Tree ensembles in scikit-learn reject
    NaN, so missing features are imputed with the training median — fitted on
    the training split only, never on test data.
    """
    usable = train[train[label_column].notna()]
    if usable.empty:
        raise ModelNotTrained("training frame has no labelled rows")

    X = usable[feature_columns].astype(float).to_numpy()
    y = usable[label_column].astype(int).to_numpy()

    estimator = build_estimator(algo, seed=seed, **overrides)

    # Pipelines in build_estimator already handle imputation.
    if algo == "logistic":
        estimator.fit(X, y)
        return estimator

    try:
        estimator.fit(X, y)
    except ValueError:
        # Tree models here cannot accept NaN; wrap with a median imputer.
        from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
        from sklearn.impute import SimpleImputer
        from sklearn.pipeline import Pipeline

        if isinstance(estimator, (HistGradientBoostingClassifier, RandomForestClassifier)):
            pipeline = Pipeline([("impute", SimpleImputer(strategy="median")), ("model", estimator)])
            pipeline.fit(X, y)
            return pipeline
        raise
    return estimator


def _final_estimator(estimator):
    """Unwrap a Pipeline to the estimator that owns ``classes_``."""
    if hasattr(estimator, "named_steps"):
        for key in ("model", "clf"):
            if key in estimator.named_steps:
                return estimator.named_steps[key]
    return estimator


def predict_proba(estimator, feature_frame: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
    """Positive-class probabilities from an arbitrary fitted estimator."""
    X = feature_frame[feature_columns].astype(float).to_numpy()
    probabilities = estimator.predict_proba(X)
    classes = list(getattr(_final_estimator(estimator), "classes_", [0, 1]))
    if 1 in classes:
        return probabilities[:, classes.index(1)]
    return probabilities[:, -1]


def extract_importance(estimator, feature_columns: list[str]) -> dict[str, float]:
    """Per-feature importance, normalised to sum to 1 when available.

    Handles the pipeline wrapper, whose real estimator lives under ``model`` or
    ``clf``. Models without a native importance (some linear models) return an
    empty mapping rather than a fabricated ranking.
    """
    target = estimator
    if hasattr(target, "named_steps"):
        for key in ("model", "clf"):
            if key in target.named_steps:
                target = target.named_steps[key]
                break

    values = getattr(target, "feature_importances_", None)
    if values is None:
        return {}

    values = np.asarray(values, dtype=float)
    if values.shape[0] != len(feature_columns):
        return {}
    total = values.sum()
    if total > 0:
        values = values / total
    return {
        name: float(value) for name, value in sorted(zip(feature_columns, values, strict=False), key=lambda kv: -kv[1])
    }
