"""Tests for index-snapshot replacement and actionable "no data" responses.

Both behaviours come from a real report: the user saw "大量 API NOT FOUND" and
"no qfq bars stored for 000025.SZ; run a data sync first".

Two distinct defects were found:

1. **Stale constituents were never removed.** `upsert_index_constituents` only
   inserted/updated, so a constituent that left an index — or a code that had been
   mis-parsed once (`000300.SZ`, the index's own code read as a Shenzhen stock) —
   stayed in the table forever and silently inflated the universe.
2. **The 404 was a dead end.** It did not name a runnable command, and could not
   distinguish "never synced" from "only the other series exists".
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from stockmaster.db import session_scope
from stockmaster.providers.base import Bar, InstrumentInfo
from stockmaster.sync import repository as repo


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Small database with one instrument that has raw bars only."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"snap-{uuid.uuid4().hex}.db")
    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    with session_scope() as session:
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code="600519.SH", name="贵州茅台", market="SH", status=1),
                InstrumentInfo(code="000001.SZ", name="平安银行", market="SZ", status=1),
            ],
        )
        repo.upsert_daily_bars(
            session,
            "600519.SH",
            [
                Bar(
                    code="600519.SH",
                    trade_date=date(2026, 10, 14) + timedelta(days=i),
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1e6,
                    amount=1e8,
                    adjust="raw",
                )
                for i in range(3)
            ],
            "raw",
        )

    yield

    session_mod.reset_engine()
    get_settings.cache_clear()


class TestIndexSnapshotReplacement:
    def test_removed_constituent_disappears(self, seeded):
        """A constituent that leaves the index must not linger."""
        as_of = date(2026, 10, 15)
        with session_scope() as session:
            repo.upsert_index_constituents(session, "000300.SH", ["600519.SH", "000001.SZ"], as_of)
            assert set(repo.index_members(session, "000300.SH")) == {
                "600519.SH",
                "000001.SZ",
            }

        # Re-sync without 000001.SZ: it left the index.
        with session_scope() as session:
            repo.upsert_index_constituents(session, "000300.SH", ["600519.SH"], as_of)

        with session_scope() as session:
            members = repo.index_members(session, "000300.SH")
        assert members == ["600519.SH"], f"stale constituent survived: {members}"

    def test_stale_index_code_is_purged(self, seeded):
        """Regression: the index's own code mis-parsed as a stock must not persist."""
        as_of = date(2026, 10, 15)
        with session_scope() as session:
            # Simulate the old bad snapshot.
            repo.upsert_index_constituents(session, "000300.SH", ["000300.SZ", "600519.SH"], as_of)

        # A correct re-sync replaces the snapshot.
        with session_scope() as session:
            repo.upsert_index_constituents(session, "000300.SH", ["600519.SH"], as_of)

        with session_scope() as session:
            members = repo.index_members(session, "000300.SH")
        assert "000300.SZ" not in members
        assert members == ["600519.SH"]

    def test_empty_codes_does_not_wipe_a_good_snapshot(self, seeded):
        """A failed upstream fetch must never destroy stored membership."""
        as_of = date(2026, 10, 15)
        with session_scope() as session:
            repo.upsert_index_constituents(session, "000300.SH", ["600519.SH"], as_of)

        with session_scope() as session:
            written = repo.upsert_index_constituents(session, "000300.SH", [], as_of)
        assert written == 0

        with session_scope() as session:
            assert repo.index_members(session, "000300.SH") == ["600519.SH"]

    def test_other_dates_are_preserved(self, seeded):
        """Replacing one date's snapshot must not touch another date's."""
        old, new = date(2026, 9, 30), date(2026, 10, 15)
        with session_scope() as session:
            repo.upsert_index_constituents(session, "000300.SH", ["600519.SH"], old)
        with session_scope() as session:
            repo.upsert_index_constituents(session, "000300.SH", ["000001.SZ"], new)

        with session_scope() as session:
            # index_members returns the latest snapshot only.
            assert repo.index_members(session, "000300.SH") == ["000001.SZ"]

        from sqlalchemy import func, select

        from stockmaster.db.models import IndexConstituent

        with session_scope() as session:
            total = session.execute(select(func.count()).select_from(IndexConstituent)).scalar()
        assert total == 2, "older snapshot should still be present"


class TestAvailableAdjusts:
    def test_reports_stored_series(self, seeded):
        with session_scope() as session:
            assert repo.available_adjusts(session, "600519.SH") == ["raw"]
            assert repo.available_adjusts(session, "000001.SZ") == []

    def test_reports_both_when_present(self, seeded):
        with session_scope() as session:
            repo.upsert_daily_bars(
                session,
                "000001.SZ",
                [
                    Bar(
                        code="000001.SZ",
                        trade_date=date(2026, 10, 14),
                        open=10.0,
                        high=10.1,
                        low=9.9,
                        close=10.05,
                        volume=1e6,
                        amount=1e7,
                        adjust="qfq",
                    )
                ],
                "qfq",
            )
        with session_scope() as session:
            assert repo.available_adjusts(session, "000001.SZ") == ["qfq"]


class TestActionableNotFound:
    def test_missing_series_names_a_runnable_command(self, seeded, client):
        # 600519.SH has raw only, so asking for qfq is a data gap.
        response = client.get("/api/v1/stocks/600519.SH/bars", params={"adjust": "qfq"})
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "sync-daily" in detail, detail
        assert "600519.SH" in detail, detail
        # And it says what *is* available.
        assert "raw" in detail, detail
        assert response.headers.get("X-StockMaster-Reason") == "data-not-synced"
        assert response.headers.get("X-StockMaster-Available-Adjusts") == "raw"

    def test_never_synced_symbol_gets_sync_instructions(self, seeded, client):
        response = client.get("/api/v1/stocks/000001.SZ/bars", params={"adjust": "qfq"})
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "sync-daily" in detail, detail
        assert "--full" in detail, detail
        assert response.headers.get("X-StockMaster-Reason") == "data-not-synced"

    def test_unknown_symbol_reports_master_problem(self, seeded, client):
        """An unknown code is a different failure and must say so."""
        response = client.get("/api/v1/stocks/999999.SH/bars", params={"adjust": "qfq"})
        assert response.status_code == 404
        assert "security master" in response.json()["detail"]

    def test_available_data_still_returns_200(self, seeded, client):
        response = client.get("/api/v1/stocks/600519.SH/bars", params={"adjust": "raw"})
        assert response.status_code == 200
        assert response.json()["count"] == 3


@pytest.fixture
def client(seeded):
    """TestClient bound to the seeded database."""
    from fastapi.testclient import TestClient

    from stockmaster.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
