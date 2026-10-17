"""Index-membership tests.

Regression cover for a bug that silently disabled the prediction pipeline: the
CSI index-constituent frame contains a column holding the **index** code itself
(e.g. ``000300`` for 沪深300). A bare six-digit string is shape-ambiguous, so
``normalize()`` classified it as Shenzhen and stored the non-existent
``000300.SZ``. That code then became the default universe, and every downstream
job reported "no data" instead of using the real universe.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pandas as pd
import pytest

from stockmaster.db import session_scope
from stockmaster.providers.base import Bar, InstrumentInfo
from stockmaster.sync import repository as repo
from stockmaster.sync.sync_service import SyncService


@pytest.fixture
def service(tmp_path, monkeypatch):
    """SyncService on a throwaway database (no provider calls in these tests)."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"idx-{uuid.uuid4().hex}.db")
    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    yield SyncService()
    session_mod.reset_engine()
    get_settings.cache_clear()


class TestIndexConstituentParsing:
    def test_index_code_is_not_stored_as_a_constituent(self, service, monkeypatch):
        """The frame's own index-code column must be filtered out."""
        # Mirrors the real CSI frame: one column is the index code, another the
        # constituent stock code.
        frame = pd.DataFrame(
            {
                "日期": ["20261015"] * 3,
                "指数代码": ["000300", "000300", "000300"],
                "成分券代码": ["600519", "000001", "300750"],
            }
        )

        class FakeAkshare:
            @staticmethod
            def index_stock_cons_csindex(symbol: str):
                return frame

        monkeypatch.setitem(__import__("sys").modules, "akshare", FakeAkshare)

        report = service.sync_index_members("000300.SH")
        assert report.failed == 0, report.errors

        with session_scope() as session:
            members = repo.index_members(session, "000300.SH")

        assert "000300.SZ" not in members, "index code was stored as a constituent"
        assert "000300.SH" not in members
        assert set(members) == {"600519.SH", "000001.SZ", "300750.SZ"}

    def test_shenzhen_index_code_is_filtered_too(self, service, monkeypatch):
        """A 399xxx index code must not be mistaken for a constituent either."""
        frame = pd.DataFrame({"A": ["399006", "399006"], "B": ["300750", "000001"]})

        class FakeAkshare:
            @staticmethod
            def index_stock_cons_csindex(symbol: str):
                return frame

        monkeypatch.setitem(__import__("sys").modules, "akshare", FakeAkshare)
        report = service.sync_index_members("399006.SZ")
        assert report.failed == 0

        with session_scope() as session:
            members = repo.index_members(session, "399006.SZ")
        assert set(members) == {"300750.SZ", "000001.SZ"}

    def test_reports_failure_when_no_codes_parsed(self, service, monkeypatch):
        class FakeAkshare:
            @staticmethod
            def index_stock_cons_csindex(symbol: str):
                return pd.DataFrame({"A": ["not-a-code"]})

        monkeypatch.setitem(__import__("sys").modules, "akshare", FakeAkshare)
        report = service.sync_index_members("000300.SH")
        assert report.failed == 1


class TestDefaultUniverseGuard:
    """The universe must only ever contain symbols that actually have bars."""

    def _seed(self, codes: list[str], with_bars: list[str]) -> None:
        days = [date(2026, 1, 5) + timedelta(days=i) for i in range(3)]
        with session_scope() as session:
            repo.upsert_instruments(
                session,
                [InstrumentInfo(code=c, name=c, market=c.split(".")[1], status=1) for c in codes],
            )
            for code in with_bars:
                repo.upsert_daily_bars(
                    session,
                    code,
                    [
                        Bar(
                            code=code,
                            trade_date=d,
                            open=10.0,
                            high=10.5,
                            low=9.5,
                            close=10.0,
                            volume=1e6,
                            amount=1e6,
                            adjust="qfq",
                        )
                        for d in days
                    ],
                    "qfq",
                )

    def test_universe_excludes_codes_without_bars(self, service):
        self._seed(["600519.SH", "000001.SZ"], with_bars=["600519.SH"])
        from stockmaster.jobs.tasks import _default_universe

        universe = _default_universe()
        assert universe == ["600519.SH"]

    def test_bogus_index_member_does_not_poison_the_universe(self, service):
        """Regression: a stored-but-unsynced index member must be skipped."""
        self._seed(["600519.SH"], with_bars=["600519.SH"])
        with session_scope() as session:
            repo.upsert_index_constituents(session, "000300.SH", ["000300.SZ", "600519.SH"], date(2026, 1, 1))

        from stockmaster.jobs.tasks import _default_universe

        universe = _default_universe()
        assert "000300.SZ" not in universe
        assert universe == ["600519.SH"]

    def test_empty_when_nothing_has_bars(self, service):
        self._seed(["600519.SH"], with_bars=[])
        from stockmaster.jobs.tasks import _default_universe

        assert _default_universe() == []
