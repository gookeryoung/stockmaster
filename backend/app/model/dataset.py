"""Prediction dataset construction.

The single most important property here is **point-in-time correctness**:

* Features at ``t`` use only data at or before ``t`` (guaranteed by the factor
  and indicator engines, and re-asserted by tests).
* Labels look *forward* by construction. That makes label availability a
  first-class concern, which is why :func:`chronological_split` **purges** the
  training tail: a training row whose label window extends into the test period
  would leak test-period information into training.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.factors.compute import compute_factors
from app.indicators.compute import compute_indicators

log = get_logger(__name__)

#: Features fed to the models. All are point-in-time by construction.
#:
#: Indicators contribute short-horizon technical state; factors contribute
#: medium-horizon cross-sectional context. ``rsi_14``/``atr_pct`` appear in both
#: engines with slightly different smoothing, so only the factor version is kept
#: in the feature set to avoid a near-duplicate column.
FEATURE_COLUMNS: tuple[str, ...] = (
    "ret_1",
    "ret_5",
    "ret_20",
    "ma5_gap",
    "ma20_gap",
    "ma60_gap",
    "macd_dif_n",
    "macd_hist_n",
    "rsi_14",
    "kdj_k",
    "kdj_d",
    "boll_pos",
    "atr_pct",
    "vol_ratio_log",
    "mom_20",
    "mom_60",
    "mom_120",
    "vol_20",
    "vol_60",
    "turnover_20",
    "amount_log_20",
    "amihud_20",
    "ma_bull",
    "breakout_20",
    "dist_high_252",
)

#: Minimum number of non-null features required to keep a row.
MIN_FEATURES_PRESENT = 12


@dataclass
class Dataset:
    """A feature/label matrix plus the metadata needed to trust it."""

    frame: pd.DataFrame
    feature_columns: list[str] = field(default_factory=list)
    horizon: int = 1
    label_column: str = "label"
    forward_return_column: str = "forward_return"

    def __len__(self) -> int:
        return len(self.frame)

    @property
    def dates(self) -> pd.Series:
        return self.frame["trade_date"]

    def describe(self) -> dict:
        base_rate = (
            float(self.frame[self.label_column].mean()) if len(self.frame) and self.label_column in self.frame else None
        )
        return {
            "rows": len(self.frame),
            "symbols": self.frame["code"].nunique() if len(self.frame) else 0,
            "date_min": str(self.dates.min()) if len(self.frame) else None,
            "date_max": str(self.dates.max()) if len(self.frame) else None,
            "horizon": self.horizon,
            "label_base_rate": base_rate,
            "feature_columns": list(self.feature_columns),
        }


def build_symbol_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Combine indicators and factors into one point-in-time feature frame.

    Args:
        bars: ascending OHLCV frame for a single symbol, with ``trade_date``.

    Returns:
        Frame with ``trade_date``, the engineered feature columns and the raw
        ``close`` (needed to derive labels).
    """
    if bars.empty:
        return pd.DataFrame()

    df = bars.sort_values("trade_date").reset_index(drop=True)
    close = df["close"].astype(float)

    indicators = compute_indicators(df)
    factors = compute_factors(df)

    out = pd.DataFrame({"trade_date": df["trade_date"]})
    out["close"] = close

    # --- short-horizon returns ---
    out["ret_1"] = close.pct_change(1)
    out["ret_5"] = close.pct_change(5)
    out["ret_20"] = close.pct_change(20)

    # --- distance from moving averages, as a scale-free fraction ---
    for window in (5, 20, 60):
        ma_value = close.rolling(window=window, min_periods=window).mean()
        out[f"ma{window}_gap"] = (close - ma_value) / ma_value.replace(0.0, np.nan)

    # --- MACD normalised by price so it is comparable across symbols ---
    out["macd_dif_n"] = indicators["macd_dif"] / close.replace(0.0, np.nan)
    out["macd_hist_n"] = indicators["macd_hist"] / close.replace(0.0, np.nan)

    out["rsi_14"] = factors["rsi_14"]
    out["kdj_k"] = indicators["kdj_k"]
    out["kdj_d"] = indicators["kdj_d"]

    # Position inside the Bollinger channel: 0 at the lower band, 1 at the upper.
    band = (indicators["boll_up"] - indicators["boll_low"]).replace(0.0, np.nan)
    out["boll_pos"] = (close - indicators["boll_low"]) / band
    out["atr_pct"] = indicators["atr_pct"]
    # Log-compress the volume ratio so extreme spikes do not dominate.
    out["vol_ratio_log"] = np.log1p(indicators["vol_ratio"].clip(lower=0.0))

    for column in (
        "mom_20",
        "mom_60",
        "mom_120",
        "vol_20",
        "vol_60",
        "turnover_20",
        "amount_log_20",
        "amihud_20",
        "ma_bull",
        "breakout_20",
        "dist_high_252",
    ):
        out[column] = factors[column]

    return out


def add_labels(features: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    """Attach forward return and binary direction label.

    ``forward_return`` is the return from ``t`` to ``t + horizon``. The label is
    1 when that return is positive. The final ``horizon`` rows of each symbol
    necessarily have no label and are dropped by the caller.
    """
    df = features.sort_values("trade_date").reset_index(drop=True)
    close = df["close"].astype(float)
    forward = close.shift(-horizon) / close - 1.0
    df["forward_return"] = forward
    df["label"] = (forward > 0).astype(float)
    # Rows without a computable label must not masquerade as negative labels.
    df.loc[forward.isna(), "label"] = np.nan
    return df


def build_dataset(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    horizon: int = 1,
    feature_columns: tuple[str, ...] = FEATURE_COLUMNS,
    min_features_present: int = MIN_FEATURES_PRESENT,
) -> Dataset:
    """Build a pooled multi-symbol dataset.

    Args:
        bars_by_symbol: canonical code -> ascending OHLCV frame.
        horizon: forward horizon in trading days.
        feature_columns: which engineered features to feed the models.
        min_features_present: drop rows with too many missing features rather
            than relying on imputation to invent them.

    Returns:
        A :class:`Dataset` whose frame is sorted by ``(trade_date, code)``.
    """
    frames: list[pd.DataFrame] = []
    for code, bars in bars_by_symbol.items():
        if bars is None or bars.empty:
            continue
        features = build_symbol_features(bars)
        if features.empty:
            continue
        features = add_labels(features, horizon=horizon)
        features["code"] = code
        frames.append(features)

    if not frames:
        return Dataset(frame=pd.DataFrame(), horizon=horizon)

    pooled = pd.concat(frames, ignore_index=True)

    # Keep only rows that have a usable label (drops each symbol's tail).
    pooled = pooled[pooled["label"].notna()]

    # Require enough real features rather than imputing mostly-missing rows.
    available = [c for c in feature_columns if c in pooled.columns]
    present = pooled[available].notna().sum(axis=1)
    pooled = pooled[present >= min_features_present]

    if pooled.empty:
        return Dataset(frame=pd.DataFrame(), horizon=horizon)

    pooled = pooled.sort_values(["trade_date", "code"]).reset_index(drop=True)

    keep = ["trade_date", "code", "close", "forward_return", "label", *available]
    pooled = pooled[keep]

    return Dataset(
        frame=pooled,
        feature_columns=list(available),
        horizon=horizon,
    )


@dataclass
class Split:
    """Chronological train/validation/test partitions plus purge bookkeeping."""

    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    horizon: int
    purged_rows: int = 0
    validation_fraction: float = 0.0

    def describe(self) -> dict:
        def span(df: pd.DataFrame) -> dict:
            if df.empty:
                return {"rows": 0, "start": None, "end": None}
            return {
                "rows": len(df),
                "start": str(df["trade_date"].min()),
                "end": str(df["trade_date"].max()),
            }

        return {
            "horizon": self.horizon,
            "purged_rows": self.purged_rows,
            "validation_fraction": self.validation_fraction,
            "train": span(self.train),
            "validation": span(self.validation),
            "test": span(self.test),
        }


def chronological_split(
    dataset: Dataset,
    *,
    test_fraction: float = 0.2,
    validation_fraction: float = 0.0,
    embargo_days: int | None = None,
) -> Split:
    """Split by calendar time, purging rows whose label window crosses forward.

    Args:
        dataset: pooled dataset sorted by date.
        test_fraction: share of the timeline reserved for the out-of-sample test.
        validation_fraction: share reserved for validation (carved before test).
        embargo_days: extra calendar gap removed between partitions. Defaults to
            a small buffer beyond the label horizon.

    The purge is the important part: the last ``horizon`` rows before the test
    boundary have labels computed from test-period prices, so they are removed
    from training/validation entirely.
    """
    frame = dataset.frame
    if frame.empty:
        empty = pd.DataFrame(columns=frame.columns)
        return Split(train=empty, validation=empty, test=empty, horizon=dataset.horizon)

    counts = frame.groupby("trade_date").size()
    dates = np.array(sorted(counts.index))
    n_dates = len(dates)

    test_count = max(1, round(n_dates * test_fraction))
    if validation_fraction and validation_fraction > 0:
        val_count = max(1, round(n_dates * validation_fraction))
    else:
        val_count = 0

    if test_count + val_count >= n_dates:
        # Not enough history to partition meaningfully; keep everything in train.
        empty = frame.iloc[0:0]
        return Split(
            train=frame.copy(),
            validation=empty,
            test=empty,
            horizon=dataset.horizon,
        )

    test_start = dates[n_dates - test_count]
    if val_count:
        validation_start = dates[n_dates - test_count - val_count]
    else:
        validation_start = test_start

    buffer = embargo_days if embargo_days is not None else 0

    train_values = dates[dates < validation_start]
    validation_values = dates[(dates >= validation_start) & (dates < test_start)] if val_count else []
    test_values = dates[dates >= test_start]

    # Purge the tail of each earlier partition: those rows' labels are computed
    # from prices inside the following partition.
    purge_train = dataset.horizon + buffer
    kept_train_values = train_values[:-purge_train] if len(train_values) > purge_train else train_values[:0]
    kept_validation_values = (
        validation_values[:-purge_train] if len(validation_values) > purge_train else validation_values[:0]
    )

    def select(days) -> pd.DataFrame:
        wanted = {str(d) for d in days}
        return frame[frame["trade_date"].astype(str).isin(wanted)]

    train = select(kept_train_values).reset_index(drop=True)
    validation = select(kept_validation_values).reset_index(drop=True)
    test = select(test_values).reset_index(drop=True)

    purged = len(frame) - len(train) - len(validation) - len(test)

    return Split(
        train=train,
        validation=validation,
        test=test,
        horizon=dataset.horizon,
        purged_rows=max(0, purged),
        validation_fraction=validation_fraction,
    )


def time_series_folds(
    frame: pd.DataFrame, *, n_splits: int = 5, horizon: int = 1
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Expanding-window walk-forward folds with a purge gap.

    Each fold trains on everything up to a date and tests on the next block, with
    the last ``horizon`` training days removed so labels cannot straddle the
    boundary. Used to report *stability* of a model rather than a single split.
    """
    dates = np.array(sorted(frame["trade_date"].unique()))
    if len(dates) < n_splits + 1:
        return []

    block = len(dates) // (n_splits + 1)
    if block < 1:
        return []

    folds: list[tuple[pd.DataFrame, pd.DataFrame]] = []
    for fold in range(n_splits):
        train_end_index = block * (fold + 1)
        test_end_index = min(train_end_index + block, len(dates))
        if test_end_index <= train_end_index:
            break

        train_dates = dates[: max(0, train_end_index - horizon)]
        test_dates = dates[train_end_index:test_end_index]
        train = frame[frame["trade_date"].astype(str).isin({str(d) for d in train_dates})]
        test = frame[frame["trade_date"].astype(str).isin({str(d) for d in test_dates})]
        if len(train) and len(test):
            folds.append((train.reset_index(drop=True), test.reset_index(drop=True)))
    return folds


def load_bars_by_symbol(session, codes: list[str], adjust: str = "qfq") -> dict[str, pd.DataFrame]:
    """Load OHLCV frames for many symbols from the database.

    Returns only symbols that actually have data, so callers can report coverage
    honestly instead of silently training on empty frames.
    """
    from app.sync import repository as repo  # imported lazily to avoid a cycle

    out: dict[str, pd.DataFrame] = {}
    for code in codes:
        rows = repo.get_bars(session, code, adjust)
        if not rows:
            continue
        out[code] = pd.DataFrame(
            {
                "trade_date": [r.trade_date for r in rows],
                "open": [r.open for r in rows],
                "high": [r.high for r in rows],
                "low": [r.low for r in rows],
                "close": [r.close for r in rows],
                "volume": [r.volume for r in rows],
                "amount": [r.amount for r in rows],
                "turn": [r.turn if r.turn is not None else np.nan for r in rows],
            }
        )
    return out
