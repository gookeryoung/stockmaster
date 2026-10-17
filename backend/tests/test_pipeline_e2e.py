"""End-to-end pipeline test (offline).

Exercises the whole chain in dependency order —
``sync → calendar → indicators → factors → train → predict → backtest → alerts`` —
against a throwaway database and a deterministic mock provider.

Why offline: the live counterpart (``scripts/acceptance_live.py``) is valuable but
depends on public data sources, which makes it unsuitable as a regression gate.
This test asserts the *integration* properties that matter — each stage consumes
what the previous one wrote, and the whole chain is re-runnable — while staying
hermetic and fast.
"""

from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path

import pytest

from app.providers.mock_provider import MockProvider
from app.providers.registry import ProviderRegistry

# 全链路 e2e（sync→…→alerts），模块级种子库 + 每例整链遍历，串行约 19s。
# 标记为 slow：默认快速门禁与 CI 排除，需要时用 `make test-slow` 运行。
pytestmark = pytest.mark.slow

_ENV_KEYS = ("SM_DATA_DIR", "SM_DB_FILENAME", "SM_SCHEDULER_ENABLED")


@pytest.fixture(scope="module")
def seeded_template(tmp_path_factory) -> Path:
    """Seed one throwaway database that every test in this module copies.

    The setup is the expensive part of this file (multi-symbol sync plus years of
    bars), and it used to be repeated for each of the nine tests. Every test still
    mutates only its own copy, so a seeded template is behaviourally equivalent and
    an order of magnitude cheaper.
    """
    base = tmp_path_factory.mktemp("pipeline-template")
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        os.environ["SM_DATA_DIR"] = str(base)
        os.environ["SM_DB_FILENAME"] = "template.db"
        os.environ["SM_SCHEDULER_ENABLED"] = "false"

        from app.config import get_settings
        from app.db import init_db
        from app.db import session as session_mod
        from app.sync.sync_service import SyncService

        get_settings.cache_clear()
        session_mod.reset_engine()
        init_db()
        _seed(SyncService(ProviderRegistry([MockProvider()], order=["mock"])))

        # 关闭连接让 SQLite 把 WAL 落盘；模板连同 sidecar 一起拷贝（见 _copy_db）。
        session_mod.reset_engine()
        get_settings.cache_clear()
        return base / "template.db"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from app.config import get_settings

        get_settings.cache_clear()


def _copy_db(source: Path, target: Path) -> None:
    """Copy a SQLite database, including its WAL sidecars when present."""
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{source}{suffix}")
        if sidecar.exists():
            shutil.copy2(sidecar, Path(f"{target}{suffix}"))


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch, seeded_template):
    """Per-test copy of the seeded database plus a mock-backed sync service."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", "e2e.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db import session as session_mod

    session_mod.reset_engine()
    _copy_db(seeded_template, tmp_path / "e2e.db")

    from app.sync.sync_service import SyncService

    yield SyncService(ProviderRegistry([MockProvider()], order=["mock"]))

    session_mod.reset_engine()
    get_settings.cache_clear()


UNIVERSE = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH"]
WINDOW_START = date(2024, 1, 1)
WINDOW_END = date(2026, 6, 30)


def _seed(sync) -> None:
    """Populate calendar, instruments and bars through the sync service."""
    sync.sync_calendar(WINDOW_START, WINDOW_END)
    from app.db import session_scope
    from app.providers.base import InstrumentInfo
    from app.sync import repository as repo

    with session_scope() as session:
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code=code, name=f"标的{index}", market=code.split(".")[1], status=1)
                for index, code in enumerate(UNIVERSE)
            ],
        )
    report = sync.sync_universe_daily(UNIVERSE, start=WINDOW_START, end=WINDOW_END, resume=False)
    assert report.failed == 0 and report.bars_written > 0


class TestPipelineDataStage:
    def test_bars_and_calendar_land(self, pipeline_env):
        _seed(pipeline_env)
        from app.db import session_scope
        from app.sync import repository as repo

        with session_scope() as session:
            for code in UNIVERSE:
                assert repo.bar_count(session, code) > 0, code
            assert repo.list_calendar(session)

    def test_sync_is_idempotent(self, pipeline_env):
        _seed(pipeline_env)
        from app.db import session_scope
        from app.sync import repository as repo

        with session_scope() as session:
            before = repo.bar_count(session)
        report = pipeline_env.sync_universe_daily(UNIVERSE, start=WINDOW_START, end=WINDOW_END, resume=False)
        with session_scope() as session:
            after = repo.bar_count(session)
        assert before == after
        assert report.failed == 0

    def test_quality_report_is_clean_after_sync(self, pipeline_env):
        """Data written by the sync chain must pass its own quality gate."""
        _seed(pipeline_env)
        from app.db import session_scope
        from app.sync.quality import check_data_quality

        with session_scope() as session:
            report = check_data_quality(session, codes=UNIVERSE, adjust="qfq")

        assert report.error_count == 0, report.as_dict()
        assert report.checked == len(UNIVERSE)


class TestPipelineAnalyticsStage:
    def test_indicators_and_factors_computed(self, pipeline_env):
        _seed(pipeline_env)
        from app.jobs.tasks import job_compute_analytics

        result = job_compute_analytics(UNIVERSE)
        assert result["symbols"] == len(UNIVERSE)

        from sqlalchemy import select

        from app.db import session_scope
        from app.db.models import Factor, Indicator

        with session_scope() as session:
            indicators = session.execute(select(Indicator)).scalars().all()
            factors = session.execute(select(Factor)).scalars().all()

        assert len(indicators) == len(UNIVERSE)
        assert len(factors) == len(UNIVERSE)
        # The analytics stage must produce usable values, not empty rows.
        assert any(i.ma20 is not None for i in indicators)
        assert any(f.score is not None for f in factors)


class TestPipelinePredictionStage:
    def test_train_then_predict_covers_universe(self, pipeline_env):
        _seed(pipeline_env)
        from app.jobs.tasks import job_compute_analytics, job_predict, job_train_models

        job_compute_analytics(UNIVERSE)

        trained = job_train_models(algo="hist_gbdt", horizon=1)
        assert trained.get("model_id"), trained
        assert trained["n_train"] > 0 and trained["n_test"] > 0

        predicted = job_predict(horizon=1)
        # Regression guard: this previously scored a single symbol because the
        # default universe resolved to a bogus index code.
        assert predicted["scored"] >= len(UNIVERSE) - 1, predicted

    def test_predictions_persisted_and_readable(self, pipeline_env):
        _seed(pipeline_env)
        from app.jobs.tasks import job_compute_analytics, job_predict, job_train_models

        job_compute_analytics(UNIVERSE)
        job_train_models(algo="hist_gbdt", horizon=1)
        job_predict(horizon=1)

        from sqlalchemy import select

        from app.db import session_scope
        from app.db.models import Prediction

        with session_scope() as session:
            rows = session.execute(select(Prediction)).scalars().all()

        assert rows
        assert all(0.0 <= r.prob_up <= 1.0 for r in rows)
        assert all(r.model_id for r in rows)


class TestPipelineBacktestStage:
    def test_score_backtest_produces_trades_and_costs(self, pipeline_env):
        """Backtest must consume stored bars on its own (not the thin factor cache)."""
        _seed(pipeline_env)

        from app.api.v1.backtest import _build_signals, _load_bars
        from app.backtest.engine import BacktestConfig, run_backtest
        from app.db import session_scope
        from app.schemas import BacktestRequest

        with session_scope() as session:
            bars = _load_bars(session, UNIVERSE)
        assert len(bars) == len(UNIVERSE)

        with session_scope() as session:
            signals = _build_signals(
                BacktestRequest(strategy="score_top_n", codes=UNIVERSE, top_n=2),
                session,
                bars,
            )

        assert signals, "score strategy produced no signals"
        # Multiple rebalance dates are the point: a single-date signal would
        # indicate the thin factor cache was being used instead of recomputation.
        assert len(signals) > 1

        result = run_backtest(bars, signals, config=BacktestConfig(), benchmark="600519.SH")
        assert not result.equity.empty
        assert result.metrics["n_trades"] > 0
        assert result.metrics["total_costs"] > 0

    def test_buy_and_hold_matches_price_direction(self, pipeline_env):
        _seed(pipeline_env)
        from app.api.v1.backtest import _load_bars
        from app.backtest.engine import BacktestConfig, buy_and_hold_signals, run_backtest
        from app.db import session_scope

        with session_scope() as session:
            bars = _load_bars(session, UNIVERSE)

        signals = buy_and_hold_signals(bars, UNIVERSE[0])
        result = run_backtest(bars, signals, config=BacktestConfig())
        assert not result.equity.empty
        assert result.metrics["n_trades"] > 0


class TestPipelineAlertStage:
    def test_rule_triggers_once_and_stays_deduped(self, pipeline_env):
        _seed(pipeline_env)
        from app.alert.engine import AlertEngine

        engine = AlertEngine()
        engine.create_rule(
            name="端到端-价格突破",
            rule_type="price_cross",
            params={"threshold": 1.0, "direction": "above"},
            scope_type="codes",
            scope_value=",".join(UNIVERSE),
            once_per_day=True,
        )

        first = engine.evaluate_all()
        assert len(first) == len(UNIVERSE)

        # The whole point of the dedup key: re-running must not duplicate.
        for _ in range(3):
            assert engine.evaluate_all() == []

        from sqlalchemy import select

        from app.db import session_scope
        from app.db.models import AlertTrigger

        with session_scope() as session:
            rows = session.execute(select(AlertTrigger)).scalars().all()
        assert len(rows) == len(UNIVERSE)


class TestFullChain:
    def test_ordered_chain_runs_end_to_end(self, pipeline_env):
        """Run every stage in dependency order and assert each one produced output."""
        _seed(pipeline_env)

        from app.alert.engine import AlertEngine
        from app.jobs.tasks import job_compute_analytics, job_predict, job_train_models

        analytics = job_compute_analytics(UNIVERSE)
        assert analytics["symbols"] == len(UNIVERSE)

        trained = job_train_models(algo="hist_gbdt", horizon=1)
        assert trained.get("model_id")

        scored = job_predict(horizon=1)
        assert scored["scored"] > 0

        from app.api.v1.backtest import _build_signals, _load_bars
        from app.backtest.engine import BacktestConfig, run_backtest
        from app.db import session_scope
        from app.schemas import BacktestRequest

        with session_scope() as session:
            bars = _load_bars(session, UNIVERSE)
            signals = _build_signals(
                BacktestRequest(strategy="prediction_top_n", codes=UNIVERSE, top_n=2),
                session,
                bars,
            )
        assert signals
        result = run_backtest(bars, signals, config=BacktestConfig())
        assert not result.equity.empty

        engine = AlertEngine()
        engine.create_rule(
            name="端到端-全链路",
            rule_type="price_cross",
            params={"threshold": 1.0, "direction": "above"},
            scope_type="codes",
            scope_value=",".join(UNIVERSE),
        )
        assert engine.evaluate_all()

    def test_chain_is_reproducible(self, pipeline_env):
        """Two runs over the same stored data must agree (deterministic seeds)."""
        _seed(pipeline_env)
        from app.jobs.tasks import job_compute_analytics, job_train_models

        job_compute_analytics(UNIVERSE)
        first = job_train_models(algo="hist_gbdt", horizon=1)
        second = job_train_models(algo="hist_gbdt", horizon=1)

        assert first["metrics"]["auc"] == pytest.approx(second["metrics"]["auc"])
        assert first["metrics"]["ic_spearman"] == pytest.approx(second["metrics"]["ic_spearman"])

    def test_quality_gate_after_full_chain(self, pipeline_env):
        """The pipeline must not corrupt the data it wrote."""
        _seed(pipeline_env)
        from app.jobs.tasks import job_compute_analytics, job_predict, job_train_models

        job_compute_analytics(UNIVERSE)
        job_train_models(algo="hist_gbdt", horizon=1)
        job_predict(horizon=1)

        from app.db import session_scope
        from app.sync.quality import check_data_quality

        with session_scope() as session:
            report = check_data_quality(session, codes=UNIVERSE, adjust="qfq")
        assert report.error_count == 0, report.as_dict()
