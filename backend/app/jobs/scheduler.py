"""APScheduler wiring for the daily pipeline.

All jobs run in the configured market timezone (Asia/Shanghai) and are only
*started* on trading days — a non-trading day short-circuits cheaply rather than
making pointless provider calls.
"""

from __future__ import annotations

from datetime import date

from app.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_scheduler = None


def is_trading_day(day: date | None = None) -> bool:
    """Whether ``day`` is an open session according to the stored calendar.

    Falls back to a weekday check when the calendar has not been synced yet, so
    the scheduler is never completely inert on a fresh install.
    """
    day = day or date.today()
    try:
        from app.db.session import session_scope
        from app.sync import repository as repo

        with session_scope() as session:
            known = repo.is_open_day(session, day)
        return bool(known)
    except Exception:  # noqa: BLE001 - a missing table must not crash startup
        return day.weekday() < 5


def _guarded(name: str, func, *, trading_day_only: bool = True):
    """Wrap a job with logging, trading-day gating and error containment."""

    def runner():
        from app.jobs.tasks import record_task_status

        if trading_day_only and not is_trading_day():
            log.info("job %s skipped: not a trading day", name)
            return
        log.info("job %s starting", name)
        try:
            result = func()
            log.info("job %s finished: %s", name, str(result)[:400])
            record_task_status(name, "done", str(result)[:2000])
        except Exception as exc:  # noqa: BLE001 - scheduler must survive job errors
            log.exception("job %s failed", name)
            record_task_status(name, "failed", f"{type(exc).__name__}: {exc}")

    return runner


def build_scheduler():
    """Create and configure the scheduler (not started)."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from app.jobs import tasks

    scheduler = BackgroundScheduler(timezone=settings.timezone)

    # Post-close chain: bars land, then analytics, then predictions, then alerts.
    scheduler.add_job(
        _guarded("calendar", tasks.job_sync_calendar),
        CronTrigger(hour=15, minute=20, day_of_week="mon-fri"),
        id="calendar", replace_existing=True,
    )
    scheduler.add_job(
        _guarded("daily", tasks.job_sync_daily),
        CronTrigger(hour=15, minute=35, day_of_week="mon-fri"),
        id="daily", replace_existing=True,
    )
    scheduler.add_job(
        _guarded("index_daily", tasks.job_sync_index_daily),
        CronTrigger(hour=16, minute=5, day_of_week="mon-fri"),
        id="index_daily", replace_existing=True,
    )
    scheduler.add_job(
        _guarded("analytics", tasks.job_compute_analytics),
        CronTrigger(hour=16, minute=30, day_of_week="mon-fri"),
        id="analytics", replace_existing=True,
    )
    scheduler.add_job(
        _guarded("predict", tasks.job_predict),
        CronTrigger(hour=16, minute=45, day_of_week="mon-fri"),
        id="predict", replace_existing=True,
    )
    scheduler.add_job(
        _guarded("alerts", tasks.job_scan_alerts),
        CronTrigger(hour=17, minute=0, day_of_week="mon-fri"),
        id="alerts", replace_existing=True,
    )

    # Intraday snapshot poll for alert freshness (trading days only).
    scheduler.add_job(
        _guarded("snapshot", tasks.job_snapshot_poll),
        IntervalTrigger(seconds=max(60, settings.alert_poll_seconds)),
        id="snapshot", replace_existing=True,
        max_instances=1,
    )

    # Weekly retrain: weekend, when no market data is changing.
    scheduler.add_job(
        _guarded("train", tasks.job_train_models, trading_day_only=False),
        CronTrigger(day_of_week="sat", hour=2, minute=0),
        id="train", replace_existing=True,
    )

    return scheduler


def start_scheduler():
    """Start the background scheduler when enabled."""
    global _scheduler
    if not settings.scheduler_enabled:
        log.info("scheduler disabled by configuration")
        return None
    if _scheduler is not None:
        return _scheduler
    try:
        _scheduler = build_scheduler()
        _scheduler.start()
        log.info("scheduler started with %d jobs", len(_scheduler.get_jobs()))
    except Exception as exc:  # noqa: BLE001 - the API must start regardless
        log.error("could not start scheduler: %s", exc)
        _scheduler = None
    return _scheduler


def stop_scheduler() -> None:
    """Shut the scheduler down (called on API shutdown)."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            log.debug("scheduler shutdown raised: %s", exc)
        _scheduler = None


def scheduler_status() -> dict:
    """Jobs and next run times, for the settings page."""
    if _scheduler is None:
        return {"running": False, "jobs": []}
    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
        )
    return {"running": True, "timezone": settings.timezone, "jobs": jobs}
