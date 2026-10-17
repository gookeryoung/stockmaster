"""Data quality check tests.

The important property is *detection*: a check that never fires is worse than no
check at all, because it breeds false confidence. Each test therefore injects one
specific defect into an otherwise clean database and asserts it is reported — and,
for the clean baseline, that nothing is reported.
"""

from __future__ import annotations

import os
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.db import session_scope
from app.db.models import DailyBar
from app.providers.base import Bar, InstrumentInfo
from app.sync import repository as repo
from app.sync.quality import (
    MIN_BARS_FOR_ANALYSIS,
    check_data_quality,
    summarize_for_log,
)

_ENV_KEYS = ("SM_DATA_DIR", "SM_DB_FILENAME")


@pytest.fixture(scope="module")
def seeded_template(tmp_path_factory) -> Path:
    """Seed one clean database that every test in this module copies.

    Every test injects a single defect into an otherwise clean database, so the
    expensive part (schema + two years of bars for three symbols) is identical
    across all of them. Seeding it once and copying the file per test keeps the
    isolation while removing ~20 repeated setups from CI.
    """
    base = tmp_path_factory.mktemp("quality-template")
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        os.environ["SM_DATA_DIR"] = str(base)
        os.environ["SM_DB_FILENAME"] = "template.db"

        from app.config import get_settings
        from app.db import session as session_mod

        get_settings.cache_clear()
        session_mod.reset_engine()
        session_mod.init_db()
        _seed_clean_database()
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


def _seed_clean_database() -> None:
    """A clean database: a real calendar plus healthy bars for a few symbols."""
    # Business-day calendar over 2 years, so coverage checks have a real basis.
    start = date(2025, 1, 1)
    sessions: list[date] = []
    day = start
    while day <= date(2026, 12, 31):
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)

    with session_scope() as session:
        repo.upsert_calendar(session, sessions, source="test")
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code="600519.SH", name="贵州茅台", market="SH", board="main", status=1),
                InstrumentInfo(code="000001.SZ", name="平安银行", market="SZ", board="main", status=1),
                InstrumentInfo(code="300750.SZ", name="宁德时代", market="SZ", board="ch", status=1),
            ],
        )
        for code, base in (("600519.SH", 100.0), ("000001.SZ", 10.0), ("300750.SZ", 200.0)):
            # 两种复权口径共用同一份 bar 列表：写库只读字段，不修改对象。
            healthy = _healthy_bars(code, sessions, base)
            repo.upsert_daily_bars(session, code, healthy, "qfq")
            repo.upsert_daily_bars(session, code, healthy, "raw")


@pytest.fixture
def seeded(tmp_path, monkeypatch, seeded_template):
    """Per-test copy of the clean database (tests mutate their own copy)."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", "quality.db")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import session as session_mod

    session_mod.reset_engine()
    _copy_db(seeded_template, tmp_path / "quality.db")

    yield

    session_mod.reset_engine()
    get_settings.cache_clear()


def _healthy_bars(code: str, sessions: list[date], base: float) -> list[Bar]:
    """Monotonic, OHLC-consistent bars that should raise no findings."""
    bars: list[Bar] = []
    close = base
    previous = base
    for index, day in enumerate(sessions):
        close = base * (1.0 + 0.0005 * index)
        bars.append(
            Bar(
                code=code,
                trade_date=day,
                open=previous,
                high=max(previous, close) * 1.005,
                low=min(previous, close) * 0.995,
                close=close,
                volume=1e6,
                amount=1e8,
                preclose=previous,
                pct_chg=(close / previous - 1.0) * 100 if previous else None,
                turn=1.0,
                is_st=False,
                adjust="qfq",
            )
        )
        previous = close
    return bars


def _scan(codes: list[str] | None = None):
    with session_scope() as session:
        return check_data_quality(session, codes=codes, adjust="qfq")


class TestCleanBaseline:
    def test_healthy_data_produces_no_findings(self, seeded):
        """No false positives: good data must come back clean."""
        report = _scan()
        assert report.checked == 3
        assert report.error_count == 0
        assert report.warning_count == 0, report.as_dict(include_symbols=True)
        assert report.healthy_symbols == 3

    def test_summary_is_one_line(self, seeded):
        text = summarize_for_log(_scan())
        assert "checked=3" in text and "errors=0" in text

    def test_report_serialises(self, seeded):
        payload = _scan().as_dict()
        for key in ("checked", "healthy", "errors", "warnings", "issue_counts", "symbols"):
            assert key in payload

    def test_only_problems_filter(self, seeded):
        _delete_symbol_bars("000001.SZ")
        payload = _scan().as_dict(only_problems=True)
        codes = {s["code"] for s in payload["symbols"]}
        assert "000001.SZ" in codes
        assert "600519.SH" not in codes


def _delete_symbol_bars(code: str) -> None:
    with session_scope() as session:
        session.query(DailyBar).filter(DailyBar.code == code).delete()


def _mutate(code: str, adjust: str, mutator) -> None:
    """Apply a mutation to every row of one symbol's series."""
    with session_scope() as session:
        rows = (
            session.query(DailyBar)
            .filter(DailyBar.code == code, DailyBar.adjust == adjust)
            .order_by(DailyBar.trade_date)
            .all()
        )
        mutator(rows)


class TestMissingHistory:
    def test_symbol_without_bars_is_missing(self, seeded):
        _delete_symbol_bars("000001.SZ")
        with session_scope() as session:
            report = check_data_quality(session, codes=["000001.SZ"], adjust="qfq")

        finding = report.symbols[0].findings[0]
        assert finding.kind == "missing_history"
        assert finding.severity == "error"
        assert not report.symbols[0].ok


class TestShortHistory:
    def test_few_bars_triggers_short_history(self, seeded):
        _mutate("600519.SH", "qfq", lambda rows: [setattr(r, "id", r.id) for r in rows])
        with session_scope() as session:
            rows = (
                session.query(DailyBar)
                .filter(DailyBar.code == "600519.SH", DailyBar.adjust == "qfq")
                .order_by(DailyBar.trade_date.desc())
                .all()
            )
            for row in rows[MIN_BARS_FOR_ANALYSIS - 5 :]:
                session.delete(row)

        report = _scan(["600519.SH"])
        kinds = {f.kind for f in report.symbols[0].findings}
        assert "short_history" in kinds


class TestStaleness:
    def test_old_last_bar_is_stale(self, seeded):
        """Drop the tail so the symbol lags the market."""
        with session_scope() as session:
            rows = (
                session.query(DailyBar)
                .filter(DailyBar.code == "600519.SH", DailyBar.adjust == "qfq")
                .order_by(DailyBar.trade_date.desc())
                .limit(30)
                .all()
            )
            for row in rows:
                session.delete(row)

        report = _scan(["600519.SH"])
        kinds = {f.kind for f in report.symbols[0].findings}
        assert "stale" in kinds

        finding = next(f for f in report.symbols[0].findings if f.kind == "stale")
        assert finding.detail["lag_days"] > 5


class TestSparseCoverage:
    def test_removing_many_sessions_is_sparse(self, seeded):
        """Delete ~30% of sessions, leaving the range intact."""
        with session_scope() as session:
            rows = (
                session.query(DailyBar)
                .filter(DailyBar.code == "000001.SZ", DailyBar.adjust == "qfq")
                .order_by(DailyBar.trade_date)
                .all()
            )
            for index, row in enumerate(rows):
                if index % 3 == 0:
                    session.delete(row)

        report = _scan(["000001.SZ"])
        kinds = {f.kind for f in report.symbols[0].findings}
        assert "sparse" in kinds

        finding = next(f for f in report.symbols[0].findings if f.kind == "sparse")
        assert finding.detail["coverage"] < 0.8


class TestOhlcValidity:
    def test_negative_price_is_invalid(self, seeded):
        def corrupt(rows):
            rows[5].close = -1.0

        _mutate("600519.SH", "qfq", corrupt)
        report = _scan(["600519.SH"])
        kinds = {f.kind for f in report.symbols[0].findings}
        assert "ohlc_invalid" in kinds

        finding = next(f for f in report.symbols[0].findings if f.kind == "ohlc_invalid")
        assert finding.severity == "error"

    def test_high_below_low_is_invalid(self, seeded):
        def corrupt(rows):
            rows[10].high = rows[10].low - 1.0

        _mutate("600519.SH", "qfq", corrupt)
        report = _scan(["600519.SH"])
        assert "ohlc_invalid" in {f.kind for f in report.symbols[0].findings}

    def test_close_above_high_is_invalid(self, seeded):
        def corrupt(rows):
            rows[12].close = rows[12].high + 5.0

        _mutate("600519.SH", "qfq", corrupt)
        report = _scan(["600519.SH"])
        assert "ohlc_invalid" in {f.kind for f in report.symbols[0].findings}


class TestExtremeReturn:
    def test_price_jump_is_flagged(self, seeded):
        """A 50% single-day move cannot be explained by a 10% limit."""

        def corrupt(rows):
            rows[100].close = rows[99].close * 1.5

        _mutate("600519.SH", "qfq", corrupt)
        report = _scan(["600519.SH"])
        kinds = {f.kind for f in report.symbols[0].findings}
        assert "extreme_return" in kinds or "qfq_discontinuity" in kinds

    def test_normal_limit_move_is_not_flagged(self, seeded):
        """A legitimate 9% move must not be reported as extreme."""

        def adjust(rows):
            rows[100].close = rows[99].close * 1.09

        _mutate("600519.SH", "qfq", adjust)
        report = _scan(["600519.SH"])
        assert "extreme_return" not in {f.kind for f in report.symbols[0].findings}


class TestAdjustmentContinuity:
    def test_spliced_series_is_detected(self, seeded):
        """Simulate the classic bug: qfq rebuilt on a different basis mid-series."""

        def splice(rows):
            for row in rows[200:]:
                row.close = row.close * 0.5
                row.open = row.open * 0.5
                row.high = row.high * 0.5
                row.low = row.low * 0.5

        _mutate("600519.SH", "qfq", splice)
        report = _scan(["600519.SH"])
        kinds = {f.kind for f in report.symbols[0].findings}
        assert "qfq_discontinuity" in kinds

        finding = next(f for f in report.symbols[0].findings if f.kind == "qfq_discontinuity")
        assert finding.severity == "error"
        assert finding.detail["examples"]


class TestSeriesPairing:
    def test_qfq_without_raw_is_orphan(self, seeded):
        _delete_symbol_bars("300750.SZ")
        with session_scope() as session:
            repo.upsert_daily_bars(
                session,
                "300750.SZ",
                _healthy_bars("300750.SZ", [date(2026, 1, 5) + timedelta(days=i) for i in range(80)], 200.0),
                "qfq",
            )

        report = _scan()
        kinds = {f.kind for f in report.global_findings}
        assert "orphan_qfq" in kinds

    def test_raw_without_qfq_is_reported(self, seeded):
        with session_scope() as session:
            session.query(DailyBar).filter(DailyBar.code == "000001.SZ", DailyBar.adjust == "qfq").delete()

        report = _scan()
        kinds = {f.kind for f in report.global_findings}
        assert "missing_qfq" in kinds


class TestEmptyDatabase:
    def test_no_bars_reports_no_data(self, seeded):
        _delete_symbol_bars("600519.SH")
        _delete_symbol_bars("000001.SZ")
        _delete_symbol_bars("300750.SZ")

        report = _scan()
        assert report.error_count >= 1
        assert any(f.kind == "no_data" for f in report.global_findings)
        assert report.checked == 0

    def test_explicit_unknown_code_is_reported_missing(self, seeded):
        """An explicitly requested code must be graded even if it has no data.

        This is deliberate: the scan's job is to surface missing data, so a code
        that is absent must be reported rather than silently skipped.
        """
        report = _scan(["999999.SH"])
        assert report.checked == 1
        assert report.symbols[0].findings[0].kind == "missing_history"

    def test_no_symbols_when_master_is_empty(self, seeded):
        """With no instruments at all there is nothing to grade."""
        from app.db.models import Instrument

        with session_scope() as session:
            session.query(Instrument).delete()

        report = _scan()
        assert report.checked == 0
        assert "no_symbols" in {f.kind for f in report.global_findings}


class TestCalendarFallback:
    def test_gap_detected_without_calendar(self, seeded):
        """With no calendar, a long hole must still be reported."""
        from app.db.models import TradeCalendar

        with session_scope() as session:
            session.query(TradeCalendar).delete()

        with session_scope() as session:
            rows = (
                session.query(DailyBar)
                .filter(DailyBar.code == "600519.SH", DailyBar.adjust == "qfq")
                .order_by(DailyBar.trade_date)
                .all()
            )
            for row in rows[300:400]:
                session.delete(row)

        report = _scan(["600519.SH"])
        kinds = {f.kind for f in report.symbols[0].findings}
        assert "gap" in kinds
        assert any(f.kind == "no_calendar" for f in report.global_findings)


class TestScanLimits:
    def test_max_symbols_caps_the_scan(self, seeded):
        with session_scope() as session:
            report = check_data_quality(session, adjust="qfq", max_symbols=2)
        assert report.checked == 2
        assert len(report.symbols) == 2

    def test_stale_threshold_is_configurable(self, seeded):
        with session_scope() as session:
            loose = check_data_quality(session, adjust="qfq", stale_days=10_000)
        assert not any(f.kind == "stale" for s in loose.symbols for f in s.findings)
