"""Model evaluation.

Guiding principle: a number is only useful if a user can tell whether it is
better than nothing. Every classification report therefore includes the
**majority-class and random baselines**, and every return series includes the
information coefficient, so an apparently high accuracy from an imbalanced or
trending test window is visibly exposed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "classification_metrics",
    "decile_spread",
    "evaluate_predictions",
    "information_coefficient",
    "quantile_returns",
]


def _safe_auc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """ROC AUC, or None when only one class is present in ``y_true``."""
    if len(np.unique(y_true)) < 2:
        return None
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y_true, scores))


def classification_metrics(y_true: np.ndarray, probabilities: np.ndarray, *, threshold: float = 0.5) -> dict:
    """Accuracy/precision/recall/AUC plus explicit random baselines.

    ``accuracy_baseline`` is the majority-class rate and ``auc_baseline`` is 0.5;
    reporting them alongside the model prevents an impressive-looking accuracy
    on a skewed test window from being read as skill.
    """
    y_true = np.asarray(y_true, dtype=float)
    probabilities = np.asarray(probabilities, dtype=float)
    if len(y_true) == 0:
        return {"n": 0}

    predictions = (probabilities >= threshold).astype(float)
    positives = float(y_true.sum())
    negatives = float(len(y_true) - positives)

    true_positive = float(((predictions == 1) & (y_true == 1)).sum())
    false_positive = float(((predictions == 1) & (y_true == 0)).sum())
    false_negative = float(((predictions == 0) & (y_true == 1)).sum())

    accuracy = float((predictions == y_true).mean())
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else None
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else None
    base_rate = positives / len(y_true)

    return {
        "n": len(y_true),
        "n_positive": int(positives),
        "n_negative": int(negatives),
        "base_rate": float(base_rate),
        "accuracy": accuracy,
        "accuracy_baseline": float(max(base_rate, 1.0 - base_rate)),
        "accuracy_edge": float(accuracy - max(base_rate, 1.0 - base_rate)),
        "auc": _safe_auc(y_true, probabilities),
        "auc_baseline": 0.5,
        "precision": precision,
        "recall": recall,
        "mean_probability": float(probabilities.mean()),
    }


def information_coefficient(scores: pd.Series, returns: pd.Series, *, method: str = "spearman") -> float | None:
    """Rank correlation between a predicted score and the realised return.

    Spearman is the default because the relationship is rarely linear; a Pearson
    IC would understate a monotonic-but-nonlinear signal. Returns ``None`` when
    either input is constant or too short.
    """
    scores = pd.Series(scores).astype(float)
    returns = pd.Series(returns).astype(float)
    mask = scores.notna() & returns.notna()
    if mask.sum() < 3:
        return None
    s = scores[mask]
    r = returns[mask]
    if s.nunique() < 2 or r.nunique() < 2:
        return None
    value = s.corr(r, method=method)
    return None if value is None or np.isnan(value) else float(value)


def quantile_returns(scores: pd.Series, returns: pd.Series, *, quantiles: int = 5) -> pd.DataFrame:
    """Mean forward return per score quantile.

    A genuine signal should produce a roughly monotonic progression across
    quantiles; a flat or non-monotonic pattern means the score is not sorting
    outcomes usefully, regardless of its IC.
    """
    scores = pd.Series(scores).astype(float)
    returns = pd.Series(returns).astype(float)
    mask = scores.notna() & returns.notna()
    if mask.sum() < quantiles:
        return pd.DataFrame(columns=["quantile", "mean_return", "count"])

    s = scores[mask]
    r = returns[mask]
    try:
        buckets = pd.qcut(s, quantiles, labels=False, duplicates="drop")
    except ValueError:
        return pd.DataFrame(columns=["quantile", "mean_return", "count"])

    grouped = r.groupby(buckets)
    return pd.DataFrame(
        {
            "quantile": list(grouped.mean().index),
            "mean_return": list(grouped.mean().values),
            "count": list(grouped.size().values),
        }
    )


def decile_spread(scores: pd.Series, returns: pd.Series, *, quantiles: int = 5) -> float | None:
    """Top-quantile minus bottom-quantile mean return.

    The practical expression of signal strength: what you would earn from the
    spread between the model's best and worst picks.
    """
    table = quantile_returns(scores, returns, quantiles=quantiles)
    if table.empty or len(table) < 2:
        return None
    table = table.sort_values("quantile")
    return float(table["mean_return"].iloc[-1] - table["mean_return"].iloc[0])


def evaluate_predictions(
    test_frame: pd.DataFrame,
    probabilities: np.ndarray,
    *,
    return_column: str = "forward_return",
    label_column: str = "label",
    threshold: float = 0.5,
    quantiles: int = 5,
) -> dict:
    """Full out-of-sample report for a prediction vector."""
    frame = test_frame.copy()
    frame["_prob"] = np.asarray(probabilities, dtype=float)

    metrics = classification_metrics(frame[label_column].to_numpy(), frame["_prob"].to_numpy(), threshold=threshold)
    metrics["ic_spearman"] = information_coefficient(frame["_prob"], frame[return_column])
    metrics["ic_pearson"] = information_coefficient(frame["_prob"], frame[return_column], method="pearson")

    table = quantile_returns(frame["_prob"], frame[return_column], quantiles=quantiles)
    metrics["quantile_table"] = table.to_dict("records") if not table.empty else []
    metrics["decile_spread"] = decile_spread(frame["_prob"], frame[return_column], quantiles=quantiles)

    # High-confidence slice: does the model actually separate outcomes where it
    # claims to be confident? This is a far stricter test than overall accuracy.
    high = frame[frame["_prob"] >= 0.6]
    low = frame[frame["_prob"] <= 0.4]
    metrics["high_confidence"] = {
        "n": len(high),
        "mean_return": float(high[return_column].mean()) if len(high) else None,
        "hit_rate": float(high[label_column].mean()) if len(high) else None,
    }
    metrics["low_confidence"] = {
        "n": len(low),
        "mean_return": float(low[return_column].mean()) if len(low) else None,
        "hit_rate": float(low[label_column].mean()) if len(low) else None,
    }

    return metrics
