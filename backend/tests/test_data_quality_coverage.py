"""Coverage-summary and CLI quality tests.

The coverage summary answers the question a user actually has — "how much of the
market do I have?" — and must stay correct even when the per-symbol scan is
capped. These tests pin that behaviour, plus the CLI's gate semantics.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.cli import main as cli_main
from app.db import session_scope
from app.providers.base import Bar, InstrumentInfo
from app.sync import repository as repo
from app.sync.quality import MIN_BARS_FOR_ANALYSIS, check_data_quality


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Three stocks: two with usable history, one with none."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"cov-{uuid.uuid4().hex}.db")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    sessions: list[date] = []
    day = date(2025, 1, 1)
    while len(sessions) < 200:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)

    with session_scope() as session:
        repo.upsert_calendar(session, sessions, source="test")
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code="600519.SH", name="贵州茅台", market="SH", status=1),
                InstrumentInfo(code="000001.SZ", name="平安银行", market="SZ", status=1),
                InstrumentInfo(code="000300.SH", name="沪深300", market="SH", is_index=True, status=1),
            ],
        )
        for code, base in (("600519.SH", 100.0), ("000001.SZ", 10.0)):
            repo.upsert_daily_bars(session, code, _bars(code, sessions, base), "qfq")
            repo.upsert_daily_bars(session, code, _bars(code, sessions, base), "raw")

    yield

    session_mod.reset_engine()
    get_settings.cache_clear()


def _bars(code: str, sessions: list[date], base: float) -> list[Bar]:
    out: list[Bar] = []
    previous = base
    for index, day in enumerate(sessions):
        close = base * (1 + 0.0005 * index)
        out.append(
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
                turn=1.0,
                adjust="qfq",
            )
        )
        previous = close
    return out


class TestCoverageSummary:
    def test_counts_usable_and_missing(self, seeded):
        with session_scope() as session:
            report = check_data_quality(session, adjust="qfq")

        coverage = report.coverage
        # The index is excluded from the stock count.
        assert coverage["stocks_total"] == 2
        assert coverage["stocks_with_series"] == 2
        assert coverage["stocks_usable"] == 2
        assert coverage["stocks_without_series"] == 0
        assert coverage["usable_share"] == pytest.approx(1.0)

    def test_missing_series_reported_as_incomplete(self, seeded, monkeypatch):
        """A stock with no bars must lower coverage and raise a global finding."""
        with session_scope() as session:
            repo.upsert_instruments(
                session,
                [InstrumentInfo(code="300750.SZ", name="宁德时代", market="SZ", status=1)],
            )

        with session_scope() as session:
            report = check_data_quality(session, adjust="qfq")

        assert report.coverage["stocks_total"] == 3
        assert report.coverage["stocks_without_series"] == 1
        assert report.coverage["usable_share"] == pytest.approx(2 / 3, abs=1e-4)
        kinds = {f.kind for f in report.global_findings}
        assert "incomplete_market" in kinds

    def test_coverage_independent_of_scan_cap(self, seeded):
        """A capped detail scan must not change whole-market coverage."""
        with session_scope() as session:
            small = check_data_quality(session, adjust="qfq", max_symbols=1)
        with session_scope() as session:
            large = check_data_quality(session, adjust="qfq", max_symbols=100)

        assert small.checked == 1
        assert small.coverage == large.coverage

    def test_short_history_counts_as_not_usable(self, seeded):
        """Fewer than MIN_BARS_FOR_ANALYSIS bars means not usable for analysis."""
        with session_scope() as session:
            session.query(repo.DailyBar).filter(
                repo.DailyBar.code == "000001.SZ", repo.DailyBar.adjust == "qfq"
            ).delete()

        sessions = []
        day = date(2025, 1, 1)
        while len(sessions) < MIN_BARS_FOR_ANALYSIS - 10:
            if day.weekday() < 5:
                sessions.append(day)
            day += timedelta(days=1)
        with session_scope() as session:
            repo.upsert_daily_bars(session, "000001.SZ", _bars("000001.SZ", sessions, 10.0), "qfq")

        with session_scope() as session:
            report = check_data_quality(session, adjust="qfq")

        assert report.coverage["stocks_with_series"] == 2
        assert report.coverage["stocks_usable"] == 1

    def test_coverage_present_when_series_empty(self, seeded):
        """With no data at all the report must still carry a coverage block."""
        with session_scope() as session:
            session.query(repo.DailyBar).delete()

        with session_scope() as session:
            report = check_data_quality(session, adjust="qfq")

        assert report.coverage["stocks_total"] == 2
        assert report.coverage["stocks_with_series"] == 0
        assert "no_data" in {f.kind for f in report.global_findings}


class TestCliQualityCommand:
    def test_prints_summary_and_succeeds(self, seeded, capsys):
        exit_code = cli_main(["data-quality", "--log-level", "ERROR"])
        output = capsys.readouterr().out

        assert exit_code == 0
        assert "数据质量体检" in output
        assert "覆盖率" in output

    def test_gate_fails_when_usable_share_below_threshold(self, seeded, capsys):
        """--min-usable-share turns the report into a usable gate."""
        exit_code = cli_main(["data-quality", "--log-level", "ERROR", "--min-usable-share", "2.0"])
        assert exit_code == 1
        assert "[失败]" in capsys.readouterr().out

    def test_gate_passes_when_threshold_met(self, seeded):
        exit_code = cli_main(["data-quality", "--log-level", "ERROR", "--min-usable-share", "0.5"])
        assert exit_code == 0

    def test_codes_filter_limits_detail(self, seeded, capsys):
        exit_code = cli_main(["data-quality", "--log-level", "ERROR", "--codes", "600519.SH"])
        output = capsys.readouterr().out
        assert exit_code == 0
        assert "逐只检查     : 1 只" in output
