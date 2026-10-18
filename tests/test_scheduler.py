"""调度器行为测试。

调度器是「无人值守的路由器」：把时间窗映射到 job，并在失败时不让整个进程倒下。
这些性质此前只能靠真实等到 15:20 才验证得到，这里用直接调用 runner 的方式覆盖。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from stockmaster.jobs import scheduler as sched


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """本模块全量隔离到临时库。

    ``_guarded`` 的 runner 会真实调用 ``record_task_status`` 写库，若某个用例
    没声明这个 fixture，就会落到默认的 ``data/stockmaster.db``——本地因为开发库
    恰好有表而「通过」，CI 上却是空库直接报 no such table。autouse 让这类
    顺序依赖无从发生。
    """
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"sched-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")

    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    yield
    session_mod.reset_engine()
    get_settings.cache_clear()


class TestTradingDay:
    def test_empty_calendar_is_treated_as_closed(self, db):
        # 日历未同步时一律视为休市：宁可少跑，也不要在空数据上打满 provider 请求
        assert sched.is_trading_day(date(2026, 1, 5)) is False
        assert sched.is_trading_day(date(2026, 1, 3)) is False

    def test_uses_stored_calendar(self, db):
        from stockmaster.db import session_scope
        from stockmaster.sync import repository as repo

        with session_scope() as session:
            repo.upsert_calendar(session, [date(2026, 1, 5), date(2026, 1, 7)], source="test")

        assert sched.is_trading_day(date(2026, 1, 5)) is True
        # 已同步的日历里没有这天（虽然是周三）→ 休市
        assert sched.is_trading_day(date(2026, 1, 6)) is False

    def test_database_failure_does_not_block_scheduler(self, db, monkeypatch):
        import stockmaster.db.session as session_mod

        def boom():
            raise RuntimeError("db gone")

        monkeypatch.setattr(session_mod, "session_scope", boom)
        # 读库失败时退回工作日判断，调度器不会整体哑掉
        assert sched.is_trading_day(date(2026, 1, 5)) is True
        assert sched.is_trading_day(date(2026, 1, 3)) is False


class TestGuardedRunner:
    def test_skips_on_non_trading_day(self, monkeypatch):
        monkeypatch.setattr(sched, "is_trading_day", lambda day=None: False)
        called: list[int] = []
        runner = sched._guarded("daily", lambda: called.append(1))
        runner()
        assert called == []

    def test_runs_and_records_success(self, monkeypatch):
        monkeypatch.setattr(sched, "is_trading_day", lambda day=None: True)
        from stockmaster.jobs import tasks

        recorded: list[tuple] = []
        monkeypatch.setattr(tasks, "record_task_status", lambda *a, **kw: recorded.append(a))

        result = sched._guarded("daily", lambda: {"bars": 10})()
        assert result is None
        assert recorded and recorded[0][0] == "daily"
        assert recorded[0][1] == "done"
        assert "bars" in recorded[0][2]

    def test_exception_is_contained_and_recorded(self, monkeypatch):
        monkeypatch.setattr(sched, "is_trading_day", lambda day=None: True)
        from stockmaster.jobs import tasks

        recorded: list[tuple] = []
        monkeypatch.setattr(tasks, "record_task_status", lambda *a, **kw: recorded.append(a))

        def explode():
            raise ValueError("provider down")

        # 不抛出去：一个 job 失败不应该让调度线程退出
        sched._guarded("predict", explode)()

        assert recorded[0][1] == "failed"
        assert "provider down" in recorded[0][2]

    def test_trading_day_only_false_runs_on_weekend(self, monkeypatch):
        monkeypatch.setattr(sched, "is_trading_day", lambda day=None: False)
        ran: list[int] = []
        sched._guarded("train", lambda: ran.append(1), trading_day_only=False)()
        assert ran == [1]


class TestBuildAndStatus:
    def test_build_registers_every_documented_job(self, db):
        scheduler = sched.build_scheduler()
        ids = {job.id for job in scheduler.get_jobs()}
        assert {"calendar", "daily", "index_daily", "analytics", "predict", "alerts", "snapshot", "train"} <= ids
        # 每个 job 都必须有中文标签，否则设置页会退化成展示 job id
        for job_id in ids:
            assert sched._job_label(job_id)["label"] != job_id

    def test_status_reports_jobs_with_human_schedule(self, db, monkeypatch):
        scheduler = sched.build_scheduler()
        monkeypatch.setattr(sched, "_scheduler", scheduler)

        status = sched.scheduler_status()
        assert status["running"] is True
        ids = {job["id"] for job in status["jobs"]}
        assert "alerts" in ids
        alerts = next(job for job in status["jobs"] if job["id"] == "alerts")
        assert alerts["label"] == "扫描预警规则"
        assert alerts["schedule"] == "交易日 17:00"

    def test_status_when_stopped(self):
        assert sched.scheduler_status() == {"running": False, "jobs": []}

    def test_start_is_a_noop_when_disabled(self, monkeypatch):
        monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
        from stockmaster.config import get_settings

        get_settings.cache_clear()
        assert sched.start_scheduler() is None
        get_settings.cache_clear()
