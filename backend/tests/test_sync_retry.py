"""Tests for backfill retry and failure-aware checkpointing.

Two behaviours are pinned here, both of which were wrong before:

1. A **failed symbol must not be recorded as completed**. Previously every symbol
   went into the resume cursor, so ``resume=True`` skipped failures forever and the
   job could never converge.
2. **Transient failures should be retried** a bounded number of times, because the
   real providers throttle and occasionally reset connections.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import ProviderError
from app.db import session_scope
from app.db.models import DataSyncTask
from app.providers.base import BaseProvider, InstrumentInfo
from app.providers.mock_provider import MockProvider, synthetic_bars
from app.providers.registry import ProviderRegistry
from app.sync.sync_service import SyncService


class FlakyProvider(BaseProvider):
    """Fails the first ``fail_times`` calls per symbol, then succeeds.

    Models the real-world case: a public data source that resets a connection or
    throttles, then works on the next attempt.
    """

    name = "flaky"

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.calls: dict[str, int] = {}

    def fetch_daily(self, code, start, end, adjust="raw"):
        self.calls[code] = self.calls.get(code, 0) + 1
        if self.calls[code] <= self.fail_times:
            raise ProviderError(f"transient failure #{self.calls[code]} for {code}")
        return synthetic_bars(code, start, end, adjust=adjust, source=self.name)

    def fetch_calendar(self, start, end):
        return [d for d in _days(start, end) if d.weekday() < 5]

    def list_instruments(self):
        return [InstrumentInfo(code="600519.SH", name="A", market="SH", status=1)]


class AlwaysFailingProvider(BaseProvider):
    """Never succeeds, to verify the retry budget is finite."""

    name = "always_failing"

    def __init__(self) -> None:
        self.calls = 0

    def fetch_daily(self, code, start, end, adjust="raw"):
        self.calls += 1
        raise ProviderError(f"permanent failure for {code}")

    def fetch_calendar(self, start, end):
        raise ProviderError("no calendar")

    def list_instruments(self):
        return []


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    """Factory for a SyncService backed by a given provider."""
    services = []

    def _make(provider) -> SyncService:
        monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SM_DB_FILENAME", f"retry-{uuid.uuid4().hex}.db")
        from app.config import get_settings

        get_settings.cache_clear()
        from app.db import session as session_mod

        session_mod.reset_engine()
        session_mod.init_db()
        service = SyncService(ProviderRegistry([provider], order=[provider.name]))
        services.append(service)
        return service

    yield _make

    from app.config import get_settings
    from app.db import session as session_mod

    session_mod.reset_engine()
    get_settings.cache_clear()


WINDOW = {"start": date(2026, 1, 1), "end": date(2026, 2, 28)}


class TestTransientRetry:
    def test_transient_failure_is_retried_and_succeeds(self, make_service):
        provider = FlakyProvider(fail_times=1)
        service = make_service(provider)

        report = service.sync_universe_daily(["600519.SH"], retries=2, retry_delay=0.0, **WINDOW)

        assert report.failed == 0, report.errors
        assert report.bars_written > 0
        # Each sync attempt fetches both raw and qfq (2 calls), so a failure plus
        # a successful retry is 4 calls.
        assert provider.calls["600519.SH"] == 4

    def test_transient_failure_without_retry_budget_stays_failed(self, make_service):
        provider = FlakyProvider(fail_times=1)
        service = make_service(provider)

        report = service.sync_universe_daily(["600519.SH"], retries=0, retry_delay=0.0, **WINDOW)

        assert report.failed == 1
        # One attempt only (no retry budget): raw + qfq.
        assert provider.calls["600519.SH"] == 2

    def test_retry_budget_is_finite(self, make_service):
        """A permanently broken symbol must not loop forever."""
        provider = AlwaysFailingProvider()
        service = make_service(provider)

        report = service.sync_universe_daily(["600519.SH"], retries=3, retry_delay=0.0, **WINDOW)

        assert report.failed == 1
        # Initial attempt + 3 retries; both raw and qfq are attempted per round.
        assert provider.calls <= 8
        assert any("仍失败" in e for e in report.errors)

    def test_successful_symbols_are_not_retried(self, make_service):
        provider = MockProvider(name="ok")
        service = make_service(provider)

        report = service.sync_universe_daily(["600519.SH", "000001.SZ"], retries=2, retry_delay=0.0, **WINDOW)
        assert report.failed == 0
        assert report.bars_written > 0


class TestFailureAwareCheckpointing:
    def test_failed_symbol_is_not_marked_complete(self, make_service):
        """The cursor must exclude failures, so a resume retries them."""
        provider = AlwaysFailingProvider()
        service = make_service(provider)

        service.sync_universe_daily(["600519.SH"], retries=0, retry_delay=0.0, **WINDOW)

        with session_scope() as session:
            task = session.execute(select(DataSyncTask).where(DataSyncTask.job == "backfill")).scalar_one()
            assert task.cursor == "", f"failed symbol leaked into cursor: {task.cursor!r}"
            assert task.status == "partial"

    def test_successful_symbol_is_marked_complete(self, make_service):
        provider = MockProvider(name="ok")
        service = make_service(provider)

        service.sync_universe_daily(["600519.SH", "000001.SZ"], retries=0, retry_delay=0.0, **WINDOW)

        with session_scope() as session:
            task = session.execute(select(DataSyncTask).where(DataSyncTask.job == "backfill")).scalar_one()
            assert set(task.cursor.split(",")) == {"600519.SH", "000001.SZ"}
            assert task.status == "done"

    def test_resume_retries_previously_failed_symbol(self, make_service):
        """Regression: a resume must re-attempt symbols that failed last time."""
        provider = FlakyProvider(fail_times=1)
        service = make_service(provider)

        first = service.sync_universe_daily(["600519.SH"], retries=0, retry_delay=0.0, **WINDOW)
        assert first.failed == 1

        # The symbol failed, so it is not in the cursor and must be retried.
        second = service.sync_universe_daily(["600519.SH"], retries=0, retry_delay=0.0, **WINDOW)
        assert second.skipped == 0
        assert second.failed == 0
        # First run failed (2 calls across raw+qfq), resume retried (2 more).
        assert provider.calls["600519.SH"] == 4
