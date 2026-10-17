"""Sync service: turn provider responses into durable, idempotent state.

Responsibilities
----------------
* **Incremental daily sync** — fetch only what is missing per symbol.
* **Resumable bulk sync** — checkpoint progress so a multi-hour full-market
  backfill can be interrupted and continued (baostock throttles and occasionally
  stalls, so long uninterrupted runs are not realistic).
* **Forward-adjustment rebuild** — detect new ex-dates and rebuild the derived
  qfq series from raw instead of appending to a stale one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select

from stockmaster.config import settings
from stockmaster.core.errors import ProviderError
from stockmaster.core.logging import get_logger
from stockmaster.core.market import default_history_start
from stockmaster.core.symbols import normalize
from stockmaster.db.models import AdjustFactor, DailyBar, DataSyncError, DataSyncTask
from stockmaster.db.session import session_scope
from stockmaster.providers.base import ADJUST_QFQ, ADJUST_RAW
from stockmaster.providers.registry import ProviderRegistry, get_registry
from stockmaster.sync import repository as repo
from stockmaster.sync.adjust import ex_date_set, recover_adjust_ratio
from stockmaster.sync.indices import parse_constituent_codes

log = get_logger(__name__)

#: Index codes used for the default "core universe" and market overview.
CORE_INDICES = ("000300.SH", "000905.SH", "000852.SH")


@dataclass
class SyncReport:
    """Outcome of a sync run."""

    job: str
    total: int = 0
    processed: int = 0
    bars_written: int = 0
    failed: int = 0
    skipped: int = 0
    adjusted_rebuilds: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "job": self.job,
            "total": self.total,
            "processed": self.processed,
            "bars_written": self.bars_written,
            "failed": self.failed,
            "skipped": self.skipped,
            "adjusted_rebuilds": self.adjusted_rebuilds,
            "errors": self.errors[:50],
            "error_count": len(self.errors),
        }


class SyncService:
    """Coordinates provider reads and database writes."""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    # ------------------------------------------------------------------ tasks
    @staticmethod
    def _task(session, job: str, target: str) -> DataSyncTask:
        task = session.execute(
            select(DataSyncTask).where(DataSyncTask.job == job, DataSyncTask.target == target)
        ).scalar_one_or_none()
        if task is None:
            task = DataSyncTask(job=job, target=target, status="pending")
            session.add(task)
            session.flush()
        return task

    @staticmethod
    def _record_error(session, job: str, code: str, provider: str, error: str) -> None:
        session.add(DataSyncError(job=job, code=code, provider=provider, error=error[:2000]))

    # ------------------------------------------------------------- calendar
    def sync_calendar(self, start: date | None = None, end: date | None = None) -> SyncReport:
        """Refresh the trading calendar (idempotent)."""
        end = end or date.today()
        start = start or (end - timedelta(days=400))
        report = SyncReport(job="calendar")
        try:
            days = self._registry.fetch_calendar(start, end)
        except ProviderError as exc:
            report.errors.append(str(exc))
            report.failed = 1
            log.error("calendar sync failed: %s", exc)
            return report
        with session_scope() as session:
            written = repo.upsert_calendar(session, days)
            task = self._task(session, "calendar", "global")
            task.status = "done"
            task.processed = written
            task.total = written
            task.message = f"{start}..{end}"
        report.processed = written
        report.bars_written = 0
        log.info("calendar synced: %d open days (%s..%s)", written, start, end)
        return report

    # ---------------------------------------------------------- instruments
    def sync_instruments(self) -> SyncReport:
        """Refresh the security master."""
        report = SyncReport(job="instruments")
        try:
            items = self._registry.list_instruments()
        except ProviderError as exc:
            report.errors.append(str(exc))
            report.failed = 1
            return report
        report.total = len(items)
        with session_scope() as session:
            written = repo.upsert_instruments(session, items)
            task = self._task(session, "instruments", "global")
            task.status = "done"
            task.processed = written
            task.total = len(items)
        report.processed = written
        log.info("instruments synced: %d rows", written)
        return report

    def sync_index_members(self, index_code: str, as_of: date | None = None) -> SyncReport:
        """Record index membership. Uses akshare's CSI source when available.

        Note: Eastmoney-backed endpoints are blocked on this host, so this uses
        ``index_stock_cons_csindex`` which is served by a different backend.
        """
        report = SyncReport(job="index_members")
        canon = normalize(index_code)
        digits = canon.split(".")[0]
        try:
            import akshare as ak

            df = ak.index_stock_cons_csindex(symbol=digits)
        except Exception as exc:
            report.errors.append(f"{type(exc).__name__}: {exc}")
            report.failed = 1
            log.warning("index member fetch failed for %s: %s", canon, exc)
            return report

        codes: list[str] = []
        if df is not None and len(df) > 0:
            codes = parse_constituent_codes(df, digits)
        if not codes:
            report.errors.append("no constituent codes parsed")
            report.failed = 1
            return report

        with session_scope() as session:
            written = repo.upsert_index_constituents(session, canon, codes, as_of or date.today(), source="csindex")
        report.total = len(codes)
        report.processed = written
        log.info("index %s members synced: %d", canon, written)
        return report

    # ----------------------------------------------------------------- daily
    def sync_symbol_daily(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
        adjusts: tuple[str, ...] = (ADJUST_RAW, ADJUST_QFQ),
        full: bool = False,
    ) -> SyncReport:
        """Sync one symbol's daily bars, incrementally by default.

        Incremental mode resumes from the last stored date per adjust mode, with
        one day of overlap so a partially-written final day gets corrected.
        ``start`` acts as an *earliest bound*: work never reaches back before it,
        but it also never re-fetches history that is already stored. Pass
        ``full=True`` to force a complete re-fetch of the requested range.
        """
        canon = normalize(code)
        end = end or date.today()
        report = SyncReport(job="daily", total=1)

        with session_scope() as session:
            for adjust in adjusts:
                if full:
                    begin = start or default_history_start(5, end)
                else:
                    last = repo.last_bar_date(session, canon, adjust)
                    if last is None:
                        begin = start or default_history_start(5, end)
                    else:
                        # Overlap one day to repair a partially written tail.
                        resume = last - timedelta(days=1)
                        begin = max(resume, start) if start is not None else resume
                if begin > end:
                    report.skipped += 1
                    continue
                try:
                    bars, _source = self._registry.fetch_daily_with_source(canon, begin, end, adjust)
                except ProviderError as exc:
                    report.failed += 1
                    err = f"{canon}/{adjust}: {exc}"
                    report.errors.append(err)
                    self._record_error(session, "daily", canon, "", str(exc))
                    continue
                written = repo.upsert_daily_bars(session, canon, bars, adjust)
                report.bars_written += written

        report.processed = 1
        return report

    # ----------------------------------------------------------- bulk/backfill
    def sync_index_daily(self, code: str, start: date, end: date, adjust: str = ADJUST_RAW) -> int:
        """Fetch and store one index's daily bars; returns rows written.

        Indices must use the dedicated index endpoint (see
        ``ProviderRegistry.fetch_index_daily``): the equity endpoint resolves a
        *stock* symbol and fails for index codes on the backup providers.

        Exposed as a service method so callers (the index sync job, tests) do not
        have to reach through the private registry attribute.
        """
        canon = normalize(code)
        bars = self._registry.fetch_index_daily(canon, start, end)
        if not bars:
            return 0
        with session_scope() as session:
            return repo.upsert_daily_bars(session, canon, bars, adjust)

    def sync_universe_daily(
        self,
        codes: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
        adjusts: tuple[str, ...] = (ADJUST_RAW, ADJUST_QFQ),
        workers: int | None = None,
        resume: bool = True,
        retries: int = 2,
        retry_delay: float = 1.0,
    ) -> SyncReport:
        """Backfill many symbols with checkpointing and bounded retries.

        Each symbol is committed independently, so an interruption loses at most
        one symbol's work and ``resume=True`` skips the ones already recorded.
        """
        end = end or date.today()
        start = start or default_history_start(5, end)
        workers = workers or settings.sync_workers
        canon_codes = [normalize(c) for c in codes]

        report = SyncReport(job="backfill", total=len(canon_codes))

        with session_scope() as session:
            task = self._task(session, "backfill", "daily")
            task.status = "running"
            task.total = len(canon_codes)
            # The cursor records only *successful* symbols. A failed symbol must
            # stay outside it, otherwise `resume=True` would skip it forever and
            # the job could never converge to "done".
            completed: set[str] = set()
            if resume and task.cursor:
                completed = {c for c in task.cursor.split(",") if c}
                report.skipped = len(completed)

        todo = [c for c in canon_codes if c not in completed]
        log.info(
            "backfill starting: %d symbols (%d already done), workers=%d",
            len(todo),
            len(completed),
            workers,
        )

        def attempt(symbols: list[str]) -> list[str]:
            """Sync each symbol once; return the ones that failed."""
            bad: list[str] = []
            for index, code in enumerate(symbols, start=1):
                sub = self.sync_symbol_daily(code, start=start, end=end, adjusts=adjusts, full=True)
                report.bars_written += sub.bars_written
                report.errors.extend(sub.errors)
                report.processed += 1
                if sub.failed:
                    bad.append(code)
                else:
                    completed.add(code)

                with session_scope() as session:
                    task = self._task(session, "backfill", "daily")
                    task.processed = report.processed
                    task.failed = len(bad)
                    task.cursor = ",".join(sorted(completed))
                    task.status = "running"
                    task.message = f"last={code}"

                if index % 50 == 0:
                    log.info(
                        "backfill progress: %d/%d (failed=%d)",
                        index,
                        len(symbols),
                        len(bad),
                    )
            return bad

        failed_codes = attempt(todo)

        # Public data sources fail transiently (throttling, resets), so retry the
        # stragglers a bounded number of times before declaring them failed.
        for round_number in range(1, max(0, retries) + 1):
            if not failed_codes:
                break
            log.info(
                "backfill retry %d/%d for %d failed symbol(s)",
                round_number,
                retries,
                len(failed_codes),
            )
            time.sleep(retry_delay)
            failed_codes = attempt(failed_codes)

        report.failed = len(failed_codes)
        if failed_codes:
            report.errors.append(f"仍失败的标的（{len(failed_codes)} 只）：{', '.join(failed_codes[:20])}")

        with session_scope() as session:
            task = self._task(session, "backfill", "daily")
            task.status = "done" if not report.failed else "partial"
            task.failed = report.failed
            task.finished_at = func.now()
        log.info(
            "backfill finished: processed=%d bars=%d failed=%d",
            report.processed,
            report.bars_written,
            report.failed,
        )
        return report

    # ------------------------------------------------------- adjust rebuild
    def rebuild_qfq_if_stale(self, code: str) -> bool:
        """Rebuild a symbol's qfq series when a new ex-date changed the factors.

        Returns ``True`` when a rebuild happened. Without this, a newly-announced
        dividend would leave the stored qfq history on the old price basis and
        create a fake jump in every derived indicator and backtest.
        """
        canon = normalize(code)
        with session_scope() as session:
            raw_rows = repo.get_bars(session, canon, ADJUST_RAW)
            qfq_rows = repo.get_bars(session, canon, ADJUST_QFQ)
            if not raw_rows or not qfq_rows:
                return False

            raw = {r.trade_date: r.close for r in raw_rows}
            stored_qfq = {r.trade_date: r.close for r in qfq_rows}
            points = recover_adjust_ratio(raw, stored_qfq)
            new_ex_dates = ex_date_set(points)

            known = {f.ex_date for f in repo.get_adjust_factors(session, canon)}
            if new_ex_dates <= known:
                return False  # nothing new; stored series is still on-basis

            log.info(
                "adjustment changed for %s (%d new ex-dates): rebuilding qfq",
                canon,
                len(new_ex_dates - known),
            )
            for ex_date in sorted(new_ex_dates):
                ratio = next((p.ratio for p in points if p.ex_date == ex_date), 1.0)
                prev = next((p.ratio for p in points if p.ex_date < ex_date), 1.0)
                step = ratio / prev if prev else 1.0
                repo.upsert_adjust_factors(
                    session,
                    canon,
                    [
                        AdjustFactor(
                            code=canon,
                            ex_date=ex_date,
                            qfq_factor=ratio,
                            hfq_factor=1.0 / step if step else 1.0,
                            source="derived",
                        )
                    ],
                )

            # Refresh qfq from the provider for the full range and replace it.
            first, last = raw_rows[0].trade_date, raw_rows[-1].trade_date

        fresh = self._registry.fetch_daily(canon, first, last, ADJUST_QFQ)
        with session_scope() as session:
            repo.delete_bars(session, canon, ADJUST_QFQ)
            repo.upsert_daily_bars(session, canon, fresh, ADJUST_QFQ)
        return True

    def rebuild_stale_adjustments(self, codes: list[str] | None = None) -> int:
        """Run :meth:`rebuild_qfq_if_stale` over a set of symbols."""
        rebuilt = 0
        targets = codes
        if targets is None:
            with session_scope() as session:
                targets = [
                    row[0]
                    for row in session.execute(
                        select(DailyBar.code).where(DailyBar.adjust == ADJUST_QFQ).distinct()
                    ).all()
                ]
        for code in targets:
            try:
                if self.rebuild_qfq_if_stale(code):
                    rebuilt += 1
            except ProviderError as exc:
                log.warning("adjust rebuild failed for %s: %s", code, exc)
        return rebuilt

    # ------------------------------------------------------------- core pool
    def core_universe(self) -> list[str]:
        """Default starter universe so the app is usable before a full backfill."""
        codes: set[str] = set()
        with session_scope() as session:
            for index_code in CORE_INDICES:
                codes.update(repo.index_members(session, index_code))
            if not codes:
                # Fall back to whatever instruments are already known.
                for item in repo.list_instruments(session, limit=300):
                    codes.add(item.code)
        return sorted(codes)

    def sync_core(self, **kwargs) -> SyncReport:
        """Sync the core universe first (fast path to a usable app)."""
        return self.sync_universe_daily(self.core_universe(), **kwargs)


def get_sync_service() -> SyncService:
    return SyncService()
