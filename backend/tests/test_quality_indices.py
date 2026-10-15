"""Index-aware quality checks.

Indices must not be graded with the stock-only adjustment rule: they carry a raw
series only, because there is nothing to adjust for. Two earlier detection attempts
were wrong and are pinned here so they cannot come back:

1. Using ``Instrument.is_index`` — the master stores only stocks, so the flag is
   never set and the check silently never fired.
2. Using ``is_index_digits()`` — matches any ``000xxx``, so it would misclassify
   real stocks like ``000001.SZ`` (平安银行).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.db import session_scope
from app.providers.base import Bar, InstrumentInfo
from app.sync import repository as repo
from app.sync.quality import check_data_quality


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Stocks in the master plus an index that only exists as a bar series."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"idx-{uuid.uuid4().hex}.db")
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
                # A 000xxx that IS a stock — must not be mistaken for an index.
                InstrumentInfo(code="000001.SZ", name="平安银行", market="SZ", status=1),
            ],
        )
        for code, base in (("600519.SH", 100.0), ("000001.SZ", 10.0)):
            repo.upsert_daily_bars(session, code, _bars(code, sessions, base), "qfq")
            repo.upsert_daily_bars(session, code, _bars(code, sessions, base), "raw")

        # Index: bars only, deliberately absent from the instrument master.
        repo.upsert_daily_bars(session, "000300.SH", _bars("000300.SH", sessions, 4000.0), "raw")

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
                adjust="raw",
            )
        )
        previous = close
    return out


class TestIndexIsNotPenalised:
    def test_index_with_only_raw_is_not_an_error(self, seeded):
        with session_scope() as session:
            report = check_data_quality(session, codes=["000300.SH"], adjust="qfq")

        symbol = report.symbols[0]
        assert symbol.ok is True, symbol.as_dict()
        kinds = {f.kind for f in symbol.findings}
        assert "index_series" in kinds
        assert "missing_qfq" not in kinds
        assert report.error_count == 0, report.as_dict()

    def test_index_absent_from_master_is_detected(self, seeded):
        """Detection must not depend on the (unused) master is_index flag."""
        with session_scope() as session:
            from sqlalchemy import select

            from app.db.models import Instrument

            flagged = session.execute(select(Instrument.code).where(Instrument.is_index.is_(True))).scalars().all()
        assert flagged == [], "fixture assumes no master rows are flagged as indices"

        with session_scope() as session:
            report = check_data_quality(session, codes=["000300.SH"], adjust="qfq")
        assert any(f.kind == "index_series" for f in report.symbols[0].findings)

    def test_399xxx_index_is_detected(self, seeded):
        with session_scope() as session:
            repo.upsert_daily_bars(
                session,
                "399006.SZ",
                _bars("399006.SZ", [date(2026, 1, 5) + timedelta(days=i) for i in range(80)], 2000.0),
                "raw",
            )
        with session_scope() as session:
            report = check_data_quality(session, codes=["399006.SZ"], adjust="qfq")
        assert report.symbols[0].ok is True
        assert any(f.kind == "index_series" for f in report.symbols[0].findings)

    def test_index_excluded_from_global_pairing(self, seeded):
        """The global series-pairing check must ignore indices."""
        with session_scope() as session:
            report = check_data_quality(session, codes=["000300.SH", "600519.SH"], adjust="qfq")
        assert "missing_qfq" not in {f.kind for f in report.global_findings}


class TestStockNotMistakenForIndex:
    def test_000001_sz_is_treated_as_a_stock(self, seeded):
        """`is_index_digits()` matches 000xxx, so a shape-only check would fail here."""
        with session_scope() as session:
            report = check_data_quality(session, codes=["000001.SZ"], adjust="qfq")

        symbol = report.symbols[0]
        # It is a real stock with both series: no index exemption, no findings.
        assert symbol.ok is True
        assert symbol.bars == 200
        assert "index_series" not in {f.kind for f in symbol.findings}

    def test_stock_missing_qfq_still_reported(self, seeded):
        """The stock-only rule must still fire for genuine stock gaps."""
        with session_scope() as session:
            from app.db.models import DailyBar

            session.query(DailyBar).filter(DailyBar.code == "600519.SH", DailyBar.adjust == "qfq").delete()

        with session_scope() as session:
            report = check_data_quality(session, codes=["600519.SH"], adjust="qfq")

        kinds = {f.kind for f in report.symbols[0].findings}
        assert "missing_qfq" in kinds
        assert report.symbols[0].ok is False

    def test_short_index_history_is_still_flagged(self, seeded):
        """An index with too few bars must not be excused entirely."""
        with session_scope() as session:
            from app.db.models import DailyBar

            session.query(DailyBar).filter(DailyBar.code == "000300.SH").delete()
            repo.upsert_daily_bars(
                session,
                "000300.SH",
                _bars("000300.SH", [date(2026, 1, 5) + timedelta(days=i) for i in range(20)], 4000.0),
                "raw",
            )

        with session_scope() as session:
            report = check_data_quality(session, codes=["000300.SH"], adjust="qfq")

        kinds = {f.kind for f in report.symbols[0].findings}
        assert "short_history" in kinds
