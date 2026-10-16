"""Regression tests: the dashboard index list and the sync job must not drift.

Three dashboard cards (深证成指, 创业板指, 中证1000) rendered empty because
`job_sync_index_daily` defaulted to a shorter, hand-written list than the
market-overview endpoint displayed. The two now share `DASHBOARD_INDICES`.
"""

from __future__ import annotations

from app.core.market import DASHBOARD_INDICES, dashboard_index_codes


class TestSharedIndexList:
    def test_dashboard_and_job_agree(self):
        """The endpoint and the sync job must cover exactly the same indices."""
        from app.api.v1.market import OVERVIEW_INDICES

        assert OVERVIEW_INDICES is DASHBOARD_INDICES
        assert dashboard_index_codes() == tuple(c for c, _ in DASHBOARD_INDICES)

    def test_covers_all_six_dashboard_cards(self):
        codes = dashboard_index_codes()
        assert len(codes) == 6, codes
        for expected in ("000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000905.SH", "000852.SH"):
            assert expected in codes, f"{expected} missing from the dashboard list"

    def test_no_duplicates(self):
        codes = dashboard_index_codes()
        assert len(codes) == len(set(codes))

    def test_job_defaults_to_the_shared_list(self):
        """The job must sync every dashboard index when called with no argument."""
        import inspect

        from app.jobs import tasks

        source = inspect.getsource(tasks.job_sync_index_daily)
        assert "dashboard_index_codes()" in source, (
            "job_sync_index_daily no longer derives its default from the shared list, "
            "so a dashboard card can silently go un-synced again"
        )
        signature = inspect.signature(tasks.job_sync_index_daily)
        assert signature.parameters["index_codes"].default is None


class TestJobSyncsEveryDashboardIndex:
    def test_mock_provider_serves_all_dashboard_indices(self, tmp_path, monkeypatch):
        """End-to-end: the job writes bars for every index the dashboard shows."""
        import uuid

        from sqlalchemy import select

        from app.db import session_scope
        from app.db.models import DailyBar
        from app.providers.mock_provider import MockProvider

        monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SM_DB_FILENAME", f"idxjob-{uuid.uuid4().hex}.db")
        from app.config import get_settings

        get_settings.cache_clear()
        from app.db import session as session_mod

        session_mod.reset_engine()
        session_mod.init_db()

        try:
            # Patch the sync service used by the job with a mock-backed one.

            class _Service:
                """Substitute for SyncService exposing only the public method the job uses."""

                def sync_index_daily(self, code, start, end, adjust="raw"):
                    from app.sync.repository import upsert_daily_bars

                    bars = MockProvider().fetch_index_daily(code, start, end)
                    with session_scope() as session:
                        return upsert_daily_bars(session, code, bars, adjust)

            from app.jobs import tasks

            def _factory():
                return _Service()

            monkeypatch.setattr(tasks, "get_sync_service", _factory)

            result = tasks.job_sync_index_daily()
            assert result["errors"] == []
            assert result["bars_written"] > 0

            with session_scope() as session:
                stored = set(session.execute(select(DailyBar.code).group_by(DailyBar.code)).scalars())
            for code in dashboard_index_codes():
                assert code in stored, f"{code} was not synced by the job"
        finally:
            session_mod.reset_engine()
            get_settings.cache_clear()
