"""日线拉取任务（``job_sync_daily``）的行为回归。

背景（两个独立的 bug 叠加，使「同步个股日线」在首次与二次使用时都失效）：

1. ``_default_universe()`` 只保留「库里已有日线」的标的。全新安装时自选股里已有
   标的但尚无任何日线，于是返回空宇宙，任务直接回 ``no universe available``，
   永远不会去拉第一根日线——先有鸡还是先有蛋。
2. ``sync_universe_daily(resume=True)`` 用 backfill 游标把「已成功」的标的永久
   跳过。第二次点同步时所有标的都在游标里，于是 ``processed: 0``，新交易日永远
   补不上。

这里固定住「今天」，用 Mock 数据源把两条路径都钉死。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from stockmaster.core.errors import ProviderError
from stockmaster.db import session_scope
from stockmaster.db.models import DailyBar, WatchlistItem
from stockmaster.providers.base import InstrumentInfo
from stockmaster.providers.mock_provider import MockProvider
from stockmaster.providers.registry import ProviderRegistry
from stockmaster.sync.sync_service import SyncService


class FakeDate(date):
    """可控的「今天」：增量拉取的 ``end`` 边界由此决定。"""

    _today = date(2026, 1, 2)  # 周五

    @classmethod
    def today(cls) -> date:
        return cls._today


class PartialFailProvider(MockProvider):
    """000001.SZ 永远失败，其余成功——用于验证失败提示。"""

    name = "partial"

    def fetch_daily(self, code, start, end, adjust="raw"):
        if code.startswith("000001"):
            raise ProviderError(f"限流：{code}")
        return super().fetch_daily(code, start, end, adjust)


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    """隔离库 + Mock 数据源，并把 job 的「今天」与重试等待固定住。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"dailyjob-{uuid.uuid4().hex}.db")
    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    # 重试是真 sleep，测试里没必要等真实秒数。
    monkeypatch.setattr("stockmaster.sync.sync_service.time.sleep", lambda *_: None)
    FakeDate._today = date(2026, 1, 2)

    import stockmaster.jobs.tasks as tasks_mod

    service = SyncService(ProviderRegistry([MockProvider()], order=["mock"]))
    monkeypatch.setattr(tasks_mod, "get_sync_service", lambda: service)
    monkeypatch.setattr(tasks_mod, "date", FakeDate)
    yield tasks_mod
    session_mod.reset_engine()
    get_settings.cache_clear()


def _seed(codes: list[str], *, watch: list[str] | None = None) -> None:
    with session_scope() as session:
        from stockmaster.sync import repository as repo

        repo.upsert_instruments(
            session, [InstrumentInfo(code=c, name=c, market=c.split(".")[1], status=1) for c in codes]
        )
        for code in watch or []:
            session.add(WatchlistItem(workspace_id=1, list_name="default", code=code, note=""))


class TestFirstRunWithoutBars:
    """回归 1：自选已有标的但无日线时，第一次同步必须真的去拉。"""

    def test_watchlist_without_bars_is_still_synced(self, tasks):
        _seed(["600519.SH"], watch=["600519.SH"])

        payload = tasks.job_sync_daily()

        assert "no universe" not in payload.get("message", "")
        assert payload["processed"] >= 1, payload
        assert payload["bars_written"] > 0, payload
        with session_scope() as session:
            from stockmaster.sync import repository as repo

            assert repo.last_bar_date(session, "600519.SH", "raw") is not None

    def test_universe_without_bars_includes_declared_symbols(self, tasks):
        """直接钉住根因：``require_bars=False`` 时自选标的即使在库无日线也入选。"""
        _seed(["600519.SH"], watch=["600519.SH"])

        assert tasks._default_universe(require_bars=False) == ["600519.SH"]
        # 默认（分析/预测用）仍保留「必须有日线」的保护。
        assert tasks._default_universe() == []


class TestIncrementalRepeat:
    """回归 2：第二次运行必须补齐新交易日，而不是被 backfill 游标永久跳过。"""

    def test_second_run_picks_up_new_sessions(self, tasks):
        _seed(["600519.SH"], watch=["600519.SH"])

        first = tasks.job_sync_daily()
        assert first["bars_written"] > 0

        # 推进「今天」到一个新的交易日：增量应补上 1/5、1/6 两根。
        FakeDate._today = date(2026, 1, 6)
        second = tasks.job_sync_daily()

        assert second["processed"] >= 1, second
        assert second["bars_written"] >= 1, second
        with session_scope() as session:
            from stockmaster.sync import repository as repo

            assert repo.last_bar_date(session, "600519.SH", "raw") == date(2026, 1, 6)

    def test_full_run_reprocesses_the_whole_window(self, tasks):
        _seed(["600519.SH"], watch=["600519.SH"])

        tasks.job_sync_daily()
        full = tasks.job_sync_daily(full=True)

        assert full["processed"] == 1
        assert full["bars_written"] > 0


class TestFailureMessages:
    """完善失败提示：空宇宙与部分失败都要给出可读、可操作的中文说明。"""

    def test_empty_universe_reports_actionable_hint(self, tasks):
        _seed([], watch=[])

        payload = tasks.job_sync_daily()

        assert payload["processed"] == 0
        message = payload["message"]
        assert "证券主数据" in message
        assert "自选" in message

    def test_partial_failure_reports_failed_count_and_hint(self, tasks, monkeypatch):
        import stockmaster.jobs.tasks as tasks_mod

        service = SyncService(ProviderRegistry([PartialFailProvider()], order=["partial"]))
        monkeypatch.setattr(tasks_mod, "get_sync_service", lambda: service)
        _seed(["600519.SH", "000001.SZ"], watch=["600519.SH", "000001.SZ"])

        payload = tasks.job_sync_daily()

        assert payload["failed"] == 1, payload
        assert payload["errors"], payload
        assert payload["bars_written"] > 0
        assert "失败" in payload["message"]
        assert "000001.SZ" in ", ".join(payload["errors"])

    def test_all_failure_reports_failure_without_claiming_success(self, tasks, monkeypatch):
        import stockmaster.jobs.tasks as tasks_mod

        class DeadProvider(MockProvider):
            name = "dead"

            def fetch_daily(self, code, start, end, adjust="raw"):
                raise ProviderError("数据源不可用")

        service = SyncService(ProviderRegistry([DeadProvider()], order=["dead"]))
        monkeypatch.setattr(tasks_mod, "get_sync_service", lambda: service)
        _seed(["600519.SH"], watch=["600519.SH"])

        payload = tasks.job_sync_daily()

        assert payload["processed"] >= 1
        assert payload["bars_written"] == 0
        assert payload["failed"] == 1
        assert "失败" in payload["message"]


def test_latest_bar_advances_tracked_in_db(tasks):
    """收口检查：库里的最新日线确实随时间推进。"""
    _seed(["600519.SH"], watch=["600519.SH"])
    tasks.job_sync_daily()
    FakeDate._today = date(2026, 1, 7)
    tasks.job_sync_daily()

    with session_scope() as session:
        from sqlalchemy import func, select

        latest = session.execute(select(func.max(DailyBar.trade_date))).scalar()
    assert latest == date(2026, 1, 7)
