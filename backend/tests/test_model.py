"""Dataset, split and model tests.

The centrepiece is the **leakage audit**: for every training row, the dates its
label depends on must all precede the test period. Asserting that mechanically is
far more reliable than eyeballing split boundaries, and it is the property that
determines whether reported performance means anything.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.errors import InsufficientData, ModelNotTrained
from app.model.dataset import (
    FEATURE_COLUMNS,
    add_labels,
    build_dataset,
    build_symbol_features,
    chronological_split,
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
    build_estimator,
    extract_importance,
    fit_model,
    predict_proba as model_predict_proba,
)
from app.model.train import train_model, score_symbols


def bars(n: int = 400, seed: int = 1, start: date = date(2024, 1, 1), code: str = "600519.SH"):
    """Synthetic OHLCV with business-day dates and a mild upward drift."""
    rng = np.random.default_rng(seed)
    dates = []
    day = start
    while len(dates) < n:
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)
    close = np.maximum(50.0 + np.cumsum(rng.normal(0.08, 1.2, n)), 1.0)
    volume = np.abs(rng.normal(1e6, 1.5e5, n))
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": close,
            "high": close * 1.012,
            "low": close * 0.988,
            "close": close,
            "volume": volume,
            "amount": close * volume,
            "turn": np.abs(rng.normal(0.012, 0.003, n)),
        }
    )


class TestSymbolFeatures:
    def test_produces_all_feature_columns(self):
        features = build_symbol_features(bars())
        for column in FEATURE_COLUMNS:
            assert column in features.columns, f"missing feature {column}"

    def test_empty_input(self):
        assert build_symbol_features(pd.DataFrame()).empty

    def test_returns_are_consistent(self):
        df = bars()
        features = build_symbol_features(df)
        expected = df["close"].pct_change(5)
        assert list(features["ret_5"]) == pytest.approx(list(expected), nan_ok=True)

    def test_ma_gap_is_zero_when_price_equals_average(self):
        df = bars(120)
        df["close"] = 100.0
        df["high"] = 100.0
        df["low"] = 100.0
        features = build_symbol_features(df)
        # Flat price sits exactly on its own average.
        assert features["ma20_gap"].dropna().abs().max() == pytest.approx(0.0)

    def test_boll_pos_bounded_for_flat_series(self):
        df = bars(120)
        df["close"] = 100.0
        df["high"] = 100.0
        df["low"] = 100.0
        features = build_symbol_features(df)
        # A zero-width channel yields NaN rather than a division blow-up.
        assert features["boll_pos"].isna().all()

    def test_no_future_leakage(self):
        """Truncating the future must not alter past feature values."""
        df = bars(360)
        cut = 300
        full = build_symbol_features(df)
        partial = build_symbol_features(df.iloc[:cut])
        for column in FEATURE_COLUMNS:
            a = full[column].iloc[:cut]
            b = partial[column]
            mask = a.notna() & b.notna()
            assert mask.sum() > 0, f"{column} had no overlapping values"
            np.testing.assert_allclose(
                a[mask].to_numpy(), b[mask].to_numpy(),
                rtol=1e-9, atol=1e-9,
                err_msg=f"{column} leaks future information",
            )


class TestLabels:
    def test_forward_return_matches_definition(self):
        df = bars(100)
        labelled = add_labels(build_symbol_features(df), horizon=5)
        # Recompute independently on the feature frame.
        close = labelled["close"].to_numpy()
        assert labelled["forward_return"].iloc[10] == pytest.approx(
            close[15] / close[10] - 1.0
        )

    def test_label_matches_sign_of_forward_return(self):
        labelled = add_labels(build_symbol_features(bars(200)), horizon=1)
        valid = labelled[labelled["forward_return"].notna()]
        assert (valid["label"] == (valid["forward_return"] > 0).astype(float)).all()

    def test_tail_rows_have_no_label(self):
        horizon = 5
        labelled = add_labels(build_symbol_features(bars(100)), horizon=horizon)
        assert labelled["forward_return"].iloc[-horizon:].isna().all()
        assert labelled["label"].iloc[-horizon:].isna().all()

    def test_label_is_nan_not_zero_when_unavailable(self):
        """A missing label must never be silently encoded as 'down'."""
        labelled = add_labels(build_symbol_features(bars(60)), horizon=3)
        tail = labelled.tail(3)
        assert tail["label"].isna().all()


class TestBuildDataset:
    def test_pools_multiple_symbols(self):
        data = {
            "600519.SH": bars(300, seed=1),
            "000001.SZ": bars(300, seed=2),
        }
        dataset = build_dataset(data, horizon=1)
        assert dataset.frame["code"].nunique() == 2
        assert len(dataset) > 0
        assert list(dataset.frame.columns[:5]) == [
            "trade_date", "code", "close", "forward_return", "label"
        ]

    def test_rows_sorted_by_date_then_code(self):
        dataset = build_dataset(
            {"600519.SH": bars(200, seed=1), "000001.SZ": bars(200, seed=2)}, horizon=1
        )
        keys = list(zip(dataset.frame["trade_date"], dataset.frame["code"]))
        assert keys == sorted(keys)

    def test_empty_input_gives_empty_dataset(self):
        assert build_dataset({}, horizon=1).frame.empty

    def test_symbols_without_data_are_skipped(self):
        dataset = build_dataset({"600519.SH": None, "000001.SZ": pd.DataFrame()}, horizon=1)
        assert dataset.frame.empty

    def test_sparse_feature_rows_dropped(self):
        dataset = build_dataset({"600519.SH": bars(120)}, horizon=1)
        # Early warm-up rows should not survive the completeness filter.
        if not dataset.frame.empty:
            present = dataset.frame[dataset.feature_columns].notna().sum(axis=1)
            assert (present >= min(12, len(dataset.feature_columns))).all()

    def test_describe_reports_base_rate(self):
        dataset = build_dataset({"600519.SH": bars(300)}, horizon=1)
        info = dataset.describe()
        assert 0.0 <= info["label_base_rate"] <= 1.0
        assert info["horizon"] == 1

    def test_all_labels_available(self):
        dataset = build_dataset({"600519.SH": bars(300)}, horizon=1)
        assert dataset.frame["label"].notna().all()
        assert dataset.frame["forward_return"].notna().all()


class TestChronologicalSplit:
    def make(self, horizon: int = 1):
        data = {
            "600519.SH": bars(500, seed=1),
            "000001.SZ": bars(500, seed=2),
            "300750.SZ": bars(500, seed=3),
        }
        return build_dataset(data, horizon=horizon)

    def test_partitions_are_time_ordered(self):
        split = chronological_split(self.make(), test_fraction=0.2)
        assert split.train["trade_date"].max() < split.test["trade_date"].min()

    def test_test_fraction_is_respected(self):
        dataset = self.make()
        split = chronological_split(dataset, test_fraction=0.25)
        total_dates = dataset.frame["trade_date"].nunique()
        test_dates = split.test["trade_date"].nunique()
        assert test_dates == pytest.approx(total_dates * 0.25, abs=2)

    def test_labels_never_reach_into_test_period(self):
        """The leakage audit: no training label may depend on test-period prices.

        For each training row, the return window spans ``horizon`` trading days
        forward. Every such date must precede the first test date.
        """
        horizon = 5
        dataset = self.make(horizon=horizon)
        split = chronological_split(dataset, test_fraction=0.2)

        test_start = split.test["trade_date"].min()
        all_dates = sorted(dataset.frame["trade_date"].unique())
        index_of = {d: i for i, d in enumerate(all_dates)}

        for row_date in split.train["trade_date"].unique():
            label_end_index = index_of[row_date] + horizon
            assert label_end_index < len(all_dates)
            label_end_date = all_dates[label_end_index]
            assert label_end_date < test_start, (
                f"training row {row_date} has a label ending {label_end_date}, "
                f"inside the test period starting {test_start}"
            )

    def test_validation_slice_precedes_test_and_is_purged(self):
        horizon = 5
        dataset = self.make(horizon=horizon)
        split = chronological_split(
            dataset, test_fraction=0.2, validation_fraction=0.2
        )
        assert not split.validation.empty
        assert split.validation["trade_date"].max() < split.test["trade_date"].min()

        test_start = split.test["trade_date"].min()
        all_dates = sorted(dataset.frame["trade_date"].unique())
        index_of = {d: i for i, d in enumerate(all_dates)}
        for row_date in split.validation["trade_date"].unique():
            label_end_date = all_dates[index_of[row_date] + horizon]
            assert label_end_date < test_start

    def test_purge_is_reported(self):
        split = chronological_split(self.make(horizon=5), test_fraction=0.2)
        assert split.purged_rows > 0, "purge should remove horizon-overlapping rows"

    def test_partitions_do_not_overlap(self):
        split = chronological_split(self.make(), test_fraction=0.2)
        train_dates = set(split.train["trade_date"])
        test_dates = set(split.test["trade_date"])
        assert not (train_dates & test_dates)

    def test_longer_horizon_purges_more(self):
        dataset = self.make(horizon=1)
        short = chronological_split(dataset, test_fraction=0.2).purged_rows
        dataset_long = self.make(horizon=20)
        long = chronological_split(dataset_long, test_fraction=0.2).purged_rows
        assert long > short

    def test_insufficient_history_falls_back_to_train_only(self):
        """When the partitions would consume the whole timeline, keep it all in train.

        A ``test_fraction`` of 1.0 leaves no dates for training, so the guard must
        decline to partition instead of producing a vacuous "out-of-sample" split
        whose metrics would be meaningless.
        """
        dataset = build_dataset({"600519.SH": bars(120)}, horizon=1)
        assert dataset.frame["trade_date"].nunique() > 2

        split = chronological_split(dataset, test_fraction=1.0)
        assert split.test.empty
        assert split.validation.empty
        # Everything is retained for training rather than silently discarded.
        assert len(split.train) == len(dataset.frame)

    def test_validation_larger_than_timeline_also_guards(self):
        dataset = build_dataset({"600519.SH": bars(120)}, horizon=1)
        split = chronological_split(
            dataset, test_fraction=0.6, validation_fraction=0.6
        )
        assert split.test.empty

    def test_empty_dataset(self):
        empty = build_dataset({}, horizon=1)
        split = chronological_split(empty, test_fraction=0.2)
        assert split.train.empty and split.test.empty

    def test_describe_shape(self):
        split = chronological_split(self.make(), test_fraction=0.2)
        info = split.describe()
        assert set(info) >= {"horizon", "train", "validation", "test", "purged_rows"}
        assert info["test"]["rows"] > 0


class TestWalkForward:
    def test_folds_are_ordered_and_disjoint(self):
        dataset = build_dataset({"600519.SH": bars(600)}, horizon=1)
        folds = time_series_folds(dataset.frame, n_splits=4, horizon=1)
        assert folds
        for train, test in folds:
            assert train["trade_date"].max() < test["trade_date"].min()

    def test_folds_purge_horizon_gap(self):
        horizon = 5
        dataset = build_dataset({"600519.SH": bars(600)}, horizon=horizon)
        folds = time_series_folds(dataset.frame, n_splits=3, horizon=horizon)
        assert folds
        all_dates = sorted(dataset.frame["trade_date"].unique())
        index_of = {d: i for i, d in enumerate(all_dates)}
        for train, test in folds:
            test_start = test["trade_date"].min()
            for row_date in train["trade_date"].unique():
                assert all_dates[index_of[row_date] + horizon] < test_start

    def test_too_few_dates_returns_no_folds(self):
        dataset = build_dataset({"600519.SH": bars(30)}, horizon=1)
        assert time_series_folds(dataset.frame, n_splits=10, horizon=1) == []


class TestEstimators:
    @pytest.mark.parametrize("algo", AVAILABLE_ALGOS)
    def test_can_fit_and_predict(self, algo):
        dataset = build_dataset({"600519.SH": bars(400, seed=5)}, horizon=1)
        if dataset.frame.empty:
            pytest.skip("no dataset")
        split = chronological_split(dataset, test_fraction=0.2)
        estimator = fit_model(algo, split.train, dataset.feature_columns)
        probabilities = model_predict_proba(
            estimator, split.test, dataset.feature_columns
        )
        assert probabilities.shape[0] == len(split.test)
        assert ((probabilities >= 0) & (probabilities <= 1)).all()

    def test_predict_proba_helper_orders_classes_correctly(self):
        """The helper must return P(label=1), not blindly the last column."""
        dataset = build_dataset({"600519.SH": bars(400, seed=21)}, horizon=1)
        split = chronological_split(dataset, test_fraction=0.2)
        estimator = fit_model("hist_gbdt", split.train, dataset.feature_columns)
        helper = model_predict_proba(estimator, split.test, dataset.feature_columns)
        raw = estimator.predict_proba(
            split.test[dataset.feature_columns].astype(float).to_numpy()
        )
        classes = list(estimator.classes_)
        assert list(helper) == pytest.approx(list(raw[:, classes.index(1)]))

    def test_unknown_algo_rejected(self):
        with pytest.raises(ValueError, match="unknown algo"):
            build_estimator("does_not_exist")

    def test_importance_sums_to_one_or_empty(self):
        dataset = build_dataset({"600519.SH": bars(400, seed=6)}, horizon=1)
        split = chronological_split(dataset, test_fraction=0.2)
        estimator = fit_model("random_forest", split.train, dataset.feature_columns)
        importance = extract_importance(estimator, dataset.feature_columns)
        assert importance
        assert sum(importance.values()) == pytest.approx(1.0, abs=1e-6)

    def test_logistic_importance_is_empty_not_fabricated(self):
        dataset = build_dataset({"600519.SH": bars(400, seed=7)}, horizon=1)
        split = chronological_split(dataset, test_fraction=0.2)
        estimator = fit_model("logistic", split.train, dataset.feature_columns)
        assert extract_importance(estimator, dataset.feature_columns) == {}

    def test_training_is_reproducible(self):
        dataset = build_dataset({"600519.SH": bars(400, seed=8)}, horizon=1)
        split = chronological_split(dataset, test_fraction=0.2)
        first = fit_model("random_forest", split.train, dataset.feature_columns, seed=42)
        second = fit_model("random_forest", split.train, dataset.feature_columns, seed=42)
        p1 = model_predict_proba(first, split.test, dataset.feature_columns)
        p2 = model_predict_proba(second, split.test, dataset.feature_columns)
        assert np.allclose(p1, p2)

    def test_empty_training_frame_raises(self):
        dataset = build_dataset({"600519.SH": bars(200)}, horizon=1)
        with pytest.raises(ModelNotTrained):
            fit_model("hist_gbdt", dataset.frame.iloc[0:0], dataset.feature_columns)


class TestEvaluation:
    def test_perfect_predictions_score_auc_one(self):
        labels = np.array([1, 1, 0, 0])
        probabilities = np.array([0.9, 0.8, 0.2, 0.1])
        metrics = classification_metrics(labels, probabilities)
        assert metrics["auc"] == pytest.approx(1.0)
        assert metrics["accuracy"] == pytest.approx(1.0)

    def test_random_baseline_is_reported(self):
        labels = np.array([1, 1, 1, 0])
        metrics = classification_metrics(labels, np.array([0.5] * 4))
        # Majority class is 1 (75%), which is the accuracy to beat.
        assert metrics["accuracy_baseline"] == pytest.approx(0.75)
        assert metrics["accuracy_edge"] == pytest.approx(0.75 - 0.75)

    def test_single_class_auc_is_none(self):
        metrics = classification_metrics(np.array([1, 1, 1]), np.array([0.6, 0.7, 0.8]))
        assert metrics["auc"] is None

    def test_empty_metrics(self):
        assert classification_metrics(np.array([]), np.array([])) == {"n": 0}

    def test_ic_detects_monotonic_relationship(self):
        scores = pd.Series(np.arange(50, dtype=float))
        returns = scores * 0.01
        assert information_coefficient(scores, returns) == pytest.approx(1.0)

    def test_ic_detects_inverse_relationship(self):
        scores = pd.Series(np.arange(50, dtype=float))
        returns = -scores * 0.01
        assert information_coefficient(scores, returns) == pytest.approx(-1.0)

    def test_ic_is_none_for_constant_scores(self):
        assert information_coefficient(pd.Series([1.0] * 10), pd.Series(range(10))) is None

    def test_ic_ignores_nan_rows(self):
        scores = pd.Series([1.0, 2.0, np.nan, 4.0, 5.0])
        returns = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert information_coefficient(scores, returns) == pytest.approx(1.0)

    def test_quantile_table_is_ordered(self):
        rng = np.random.default_rng(3)
        scores = pd.Series(rng.normal(size=300))
        returns = scores * 0.01 + pd.Series(rng.normal(scale=0.005, size=300))
        table = quantile_returns(scores, returns, quantiles=5)
        assert len(table) == 5
        assert list(table["quantile"]) == sorted(table["quantile"])

    def test_decile_spread_positive_for_real_signal(self):
        rng = np.random.default_rng(4)
        scores = pd.Series(rng.normal(size=400))
        returns = scores * 0.02
        assert decile_spread(scores, returns) > 0

    def test_spread_is_none_for_tiny_sample(self):
        assert decile_spread(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0])) is None

    def test_evaluate_predictions_reports_confidence_slices(self):
        frame = pd.DataFrame(
            {
                "label": [1, 1, 0, 0, 1, 0],
                "forward_return": [0.02, 0.03, -0.01, -0.02, 0.01, -0.03],
            }
        )
        probabilities = np.array([0.9, 0.7, 0.3, 0.1, 0.55, 0.45])
        metrics = evaluate_predictions(frame, probabilities)
        assert metrics["high_confidence"]["n"] == 2
        assert metrics["low_confidence"]["n"] == 2
        assert "ic_spearman" in metrics


class TestTrainModel:
    def test_trains_and_reports_honest_metrics(self):
        data = {"600519.SH": bars(600, seed=11), "000001.SZ": bars(600, seed=12)}
        dataset = build_dataset(data, horizon=1)
        model = train_model(dataset, algo="hist_gbdt", persist=False)
        assert "auc" in model.metrics
        # Baseline must be present so the number is interpretable.
        assert "accuracy_baseline" in model.metrics
        assert model.metrics["split"]["purged_rows"] >= 0
        assert model.n_train > 0 and model.n_test > 0

    def test_predictions_are_probabilities(self):
        data = {"600519.SH": bars(600, seed=13)}
        dataset = build_dataset(data, horizon=1)
        model = train_model(dataset, algo="random_forest", persist=False)
        split = chronological_split(dataset, test_fraction=0.2)
        probabilities = model.predict_proba(split.test)
        assert ((probabilities >= 0) & (probabilities <= 1)).all()

    def test_missing_feature_column_raises(self):
        data = {"600519.SH": bars(600, seed=14)}
        dataset = build_dataset(data, horizon=1)
        model = train_model(dataset, algo="hist_gbdt", persist=False)
        stripped = dataset.frame.drop(columns=[model.feature_columns[0]])
        with pytest.raises(ModelNotTrained):
            model.predict_proba(stripped)

    def test_empty_dataset_raises(self):
        empty = build_dataset({}, horizon=1)
        with pytest.raises(InsufficientData):
            train_model(empty, persist=False)

    def test_persist_and_reload_roundtrip(self, tmp_path, monkeypatch):
        import uuid

        monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SM_DB_FILENAME", f"model-{uuid.uuid4().hex}.db")
        from app.config import get_settings

        get_settings.cache_clear()
        from app.db import session as session_mod

        session_mod.reset_engine()
        session_mod.init_db()
        try:
            from app.model.train import load_active_model

            data = {"600519.SH": bars(600, seed=15)}
            dataset = build_dataset(data, horizon=1)
            model = train_model(dataset, algo="hist_gbdt", persist=True)

            reloaded = load_active_model(horizon=1)
            assert reloaded.model_id == model.model_id
            assert reloaded.feature_columns == model.feature_columns
            # Reloaded model must give identical predictions.
            split = chronological_split(dataset, test_fraction=0.2)
            assert np.allclose(
                model.predict_proba(split.test), reloaded.predict_proba(split.test)
            )
        finally:
            session_mod.reset_engine()
            get_settings.cache_clear()

    def test_scoring_returns_one_row_per_symbol(self):
        data = {"600519.SH": bars(600, seed=16), "000001.SZ": bars(600, seed=17)}
        dataset = build_dataset(data, horizon=1)
        model = train_model(dataset, algo="hist_gbdt", persist=False)
        records = score_symbols(model, data, persist=False)
        assert len(records) == 2
        assert all(0.0 <= r["prob_up"] <= 1.0 for r in records)
        assert all(r["horizon"] == 1 for r in records)
