"""Sync service tests: idempotency, incrementality, checkpointing, rebuilds."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.db import session_scope
from app.db.models import DailyBar, DataSyncTask
from app.providers.base import ADJUST_QFQ, ADJUST_RAW
from app.providers.mock_provider import MockProvider, synthetic_bars
from app.providers.registry import ProviderRegistry
from app.sync import repository as repo
from app.sync.sync_service import SyncService


@pytest.fixture
def service(tmp_path, monkeypatch):
    """SyncService wired to a mock provider and a throwaway database.

    The database filename is unique per test so a shared ``tmp_path`` can never
    leak state between tests.
    """
    import uuid

    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"sync-{uuid.uuid4().hex}.db")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    registry = ProviderRegistry([MockProvider()], order=["mock"])
    yield SyncService(registry)
    session_mod.reset_engine()
    get_settings.cache_clear()


WINDOW = (date(2026, 1, 1), date(2026, 3, 31))


def win():
    """``start``/``end`` keyword args for the service methods."""
    return {"start": WINDOW[0], "end": WINDOW[1]}


class TestCalendarSync:
    def test_calendar_persists_open_days(self, service):
        report = service.sync_calendar(*WINDOW)
        assert report.failed == 0
        assert report.processed > 0
        with session_scope() as session:
            days = repo.list_calendar(session)
        assert days
        assert all(d.weekday() < 5 for d in days)

    def test_calendar_is_idempotent(self, service):
        service.sync_calendar(*WINDOW)
        with session_scope() as session:
            first = len(repo.list_calendar(session))
        service.sync_calendar(*WINDOW)
        with session_scope() as session:
            second = len(repo.list_calendar(session))
        assert first == second

    def test_task_status_recorded(self, service):
        service.sync_calendar(*WINDOW)
        with session_scope() as session:
            task = session.execute(
                __import__("sqlalchemy").select(DataSyncTask).where(
                    DataSyncTask.job == "calendar"
                )
            ).scalar_one()
            assert task.status == "done"


class TestInstrumentSync:
    def test_instruments_persist(self, service):
        report = service.sync_instruments()
        assert report.processed == 2
        with session_scope() as session:
            items = repo.list_instruments(session)
        assert {i.code for i in items} == {"600519.SH", "000001.SZ"}

    def test_instrument_name_not_blanked_on_resync(self, service):
        service.sync_instruments()
        with session_scope() as session:
            repo.upsert_instruments(
                session,
                [__import__("app.providers.base", fromlist=["InstrumentInfo"]).InstrumentInfo(
                    code="600519.SH", name="", market="SH"
                )],
            )
        with session_scope() as session:
            item = repo.get_instrument(session, "600519.SH")
            # A blank incoming name must not erase a known one.
            assert item.name == "Mock A"


class TestDailySync:
    def test_symbol_sync_writes_both_adjust_modes(self, service):
        report = service.sync_symbol_daily("600519.SH", **win())
        assert report.failed == 0
        assert report.bars_written > 0
        with session_scope() as session:
            raw = repo.get_bars(session, "600519.SH", ADJUST_RAW)
            qfq = repo.get_bars(session, "600519.SH", ADJUST_QFQ)
        assert raw and qfq
        assert len(raw) == len(qfq)

    def test_resync_is_idempotent(self, service):
        service.sync_symbol_daily("600519.SH", **win())
        with session_scope() as session:
            count_first = repo.bar_count(session, "600519.SH")
        service.sync_symbol_daily("600519.SH", **win())
        with session_scope() as session:
            count_second = repo.bar_count(session, "600519.SH")
        assert count_first == count_second

    def test_incremental_only_adds_new_days(self, service, monkeypatch):
        """A second sync over a longer window must add only the new tail."""
        service.sync_symbol_daily("600519.SH", start=date(2026, 1, 1), end=date(2026, 1, 31))
        with session_scope() as session:
            first = repo.bar_count(session, "600519.SH", ADJUST_RAW)

        # Extend the horizon; incremental mode resumes near the last date.
        service.sync_symbol_daily("600519.SH", start=date(2026, 1, 1), end=date(2026, 3, 31))
        with session_scope() as session:
            after = repo.bar_count(session, "600519.SH", ADJUST_RAW)
        assert after > first

        expected = len(synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 3, 31)))
        assert after == expected

    def test_full_flag_refetches_whole_range(self, service):
        service.sync_symbol_daily(
            "600519.SH", start=date(2026, 1, 1), end=date(2026, 1, 15)
        )
        report = service.sync_symbol_daily(
            "600519.SH", start=date(2026, 1, 1), end=date(2026, 2, 28), full=True
        )
        assert report.bars_written > 0
        with session_scope() as session:
            count = repo.bar_count(session, "600519.SH", ADJUST_RAW)
        assert count == len(synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 2, 28)))

    def test_window_is_not_extended_past_end(self, service):
        """An explicit ``end`` must bound the fetch, regardless of today's date."""
        service.sync_symbol_daily(
            "600519.SH", start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
        with session_scope() as session:
            last = repo.last_bar_date(session, "600519.SH", ADJUST_RAW)
        assert last is not None and last <= date(2026, 1, 31)

    def test_incremental_ignores_start_earlier_than_existing(self, service):
        """An earlier ``start`` must not trigger a needless full re-fetch."""
        service.sync_symbol_daily(
            "600519.SH", start=date(2026, 2, 1), end=date(2026, 2, 28)
        )
        with session_scope() as session:
            first = repo.bar_count(session, "600519.SH", ADJUST_RAW)
        # Same window again, but with a much earlier start bound.
        report = service.sync_symbol_daily(
            "600519.SH", start=date(2020, 1, 1), end=date(2026, 2, 28)
        )
        with session_scope() as session:
            after = repo.bar_count(session, "600519.SH", ADJUST_RAW)
        # Count is unchanged because the resolved begin stays at the resume point.
        assert after == first
        assert report.failed == 0

    def test_upsert_updates_existing_row(self, service):
        """Re-syncing a date with a changed price must overwrite, not duplicate."""
        service.sync_symbol_daily("600519.SH", **win())
        with session_scope() as session:
            bar = repo.get_bars(session, "600519.SH", ADJUST_RAW)[0]
            original_date = bar.trade_date
            original_close = bar.close

        bars = synthetic_bars("600519.SH", original_date, original_date)
        bars[0].close = original_close + 5.0
        bars[0].high = original_close + 10.0
        with session_scope() as session:
            repo.upsert_daily_bars(session, "600519.SH", bars, ADJUST_RAW)

        with session_scope() as session:
            rows = repo.get_bars(session, "600519.SH", ADJUST_RAW)
            target = [r for r in rows if r.trade_date == original_date]
        assert len(target) == 1
        assert target[0].close == pytest.approx(original_close + 5.0)

    def test_failure_is_recorded_and_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SM_DB_FILENAME", "fail.db")
        from app.config import get_settings

        get_settings.cache_clear()
        from app.db import session as session_mod

        session_mod.reset_engine()
        session_mod.init_db()
        try:
            from app.providers.mock_provider import FailingProvider

            svc = SyncService(ProviderRegistry([FailingProvider()], order=["failing"]))
            report = svc.sync_symbol_daily("600519.SH", **win())
            assert report.failed >= 1
            assert report.errors
            with session_scope() as session:
                from app.db.models import DataSyncError

                errors = session.execute(
                    __import__("sqlalchemy").select(DataSyncError)
                ).scalars().all()
                assert errors
        finally:
            session_mod.reset_engine()
            get_settings.cache_clear()


class TestBulkSync:
    def test_backfill_checkpoints_and_resumes(self, service):
        codes = ["600519.SH", "000001.SZ"]
        report = service.sync_universe_daily(codes, **win())
        assert report.failed == 0
        assert report.processed == 2

        with session_scope() as session:
            task = session.execute(
                __import__("sqlalchemy").select(DataSyncTask).where(
                    DataSyncTask.job == "backfill"
                )
            ).scalar_one()
            assert task.status == "done"
            assert set(task.cursor.split(",")) == set(codes)

        # Resuming must skip everything already recorded.
        again = service.sync_universe_daily(codes, resume=True, **win())
        assert again.skipped == 2
        assert again.processed == 0

    def test_resume_false_reprocesses(self, service):
        codes = ["600519.SH"]
        service.sync_universe_daily(codes, **win())
        again = service.sync_universe_daily(codes, resume=False, **win())
        assert again.processed == 1

    def test_bulk_is_idempotent_in_bar_count(self, service):
        codes = ["600519.SH", "000001.SZ"]
        service.sync_universe_daily(codes, **win())
        with session_scope() as session:
            first = repo.bar_count(session)
        service.sync_universe_daily(codes, resume=False, **win())
        with session_scope() as session:
            second = repo.bar_count(session)
        assert first == second

    def test_core_universe_falls_back_to_known_instruments(self, service):
        service.sync_instruments()
        codes = service.core_universe()
        assert codes  # falls back when no index membership exists
        assert all(c in {"600519.SH", "000001.SZ"} for c in codes)


class TestAdjustRebuild:
    def test_no_rebuild_when_factors_unchanged(self, service):
        service.sync_symbol_daily("600519.SH", **win())
        # Mock provider returns qfq == raw, so the ratio never steps: no ex-dates.
        assert service.rebuild_qfq_if_stale("600519.SH") is False

    def test_rebuild_detects_new_ex_date_and_replaces_series(self, service):
        """A changed qfq basis must trigger a full replacement, not an append."""
        service.sync_symbol_daily("600519.SH", **win())

        code = "600519.SH"
        with session_scope() as session:
            raw_rows = repo.get_bars(session, code, ADJUST_RAW)
            raw = {r.trade_date: r.close for r in raw_rows}
            assert len(raw) > 20
            # Simulate a corporate action: the provider's qfq series now steps
            # down by 10% from the midpoint onward.
            days = sorted(raw)
            step_date = days[len(days) // 2]
            repo.delete_bars(session, code, ADJUST_QFQ)
            shifted = []
            for day in days:
                bar = next(r for r in raw_rows if r.trade_date == day)
                scale = 0.9 if day >= step_date else 1.0
                shifted.append(
                    type(bar)(
                        code=code,
                        trade_date=day,
                        adjust=ADJUST_QFQ,
                        open=bar.open * scale,
                        high=bar.high * scale,
                        low=bar.low * scale,
                        close=bar.close * scale,
                        volume=bar.volume,
                        amount=bar.amount,
                    )
                )
            repo.upsert_daily_bars(session, code, shifted, ADJUST_QFQ)

        # The stored series now implies a ratio step the factor table lacks.
        assert service.rebuild_qfq_if_stale(code) is True

        with session_scope() as session:
            factors = repo.get_adjust_factors(session, code)
        assert factors, "expected a derived adjust factor to be recorded"

    def test_rebuild_is_not_repeated(self, service):
        service.sync_symbol_daily("600519.SH", **win())
        code = "600519.SH"
        with session_scope() as session:
            raw_rows = repo.get_bars(session, code, ADJUST_RAW)
            days = sorted(r.trade_date for r in raw_rows)
            step_date = days[len(days) // 2]
            repo.delete_bars(session, code, ADJUST_QFQ)
            shifted = []
            for bar in raw_rows:
                scale = 0.9 if bar.trade_date >= step_date else 1.0
                shifted.append(
                    type(bar)(
                        code=code, trade_date=bar.trade_date, adjust=ADJUST_QFQ,
                        open=bar.open * scale, high=bar.high * scale,
                        low=bar.low * scale, close=bar.close * scale,
                        volume=bar.volume, amount=bar.amount,
                    )
                )
            repo.upsert_daily_bars(session, code, shifted, ADJUST_QFQ)

        # First pass records the factor; the mock provider then serves an
        # unadjusted series, so no *further* factor should ever be detected.
        service.rebuild_qfq_if_stale(code)
        with session_scope() as session:
            after_first = len(repo.get_adjust_factors(session, code))
        service.rebuild_qfq_if_stale(code)
        with session_scope() as session:
            after_second = len(repo.get_adjust_factors(session, code))
        assert after_first == after_second


class TestIndexMembers:
    def test_index_members_roundtrip(self, service):
        with session_scope() as session:
            repo.upsert_index_constituents(
                session, "000905.SH", ["600519.SH", "000001.SZ"], date(2026, 1, 1)
            )
        with session_scope() as session:
            members = repo.index_members(session, "000905.SH")
        assert set(members) == {"600519.SH", "000001.SZ"}

    def test_index_members_uses_latest_snapshot(self, service):
        with session_scope() as session:
            repo.upsert_index_constituents(
                session, "000852.SH", ["600519.SH"], date(2026, 1, 1)
            )
            repo.upsert_index_constituents(
                session, "000852.SH", ["000001.SZ"], date(2026, 6, 1)
            )
        with session_scope() as session:
            members = repo.index_members(session, "000852.SH")
        assert members == ["000001.SZ"]

    def test_unknown_index_returns_empty(self, service):
        with session_scope() as session:
            assert repo.index_members(session, "999999.SH") == []
