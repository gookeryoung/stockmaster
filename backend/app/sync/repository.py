"""Repository: idempotent persistence for reference and market data.

Upserts use SQLite's native ``ON CONFLICT`` so re-running a sync never creates
duplicates and never requires a read-modify-write round trip. That property is
what makes the whole pipeline safely re-runnable and resumable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    AdjustFactor,
    DailyBar,
    IndexConstituent,
    Instrument,
    TradeCalendar,
)
from app.providers.base import Bar, InstrumentInfo

log = get_logger(__name__)


def _chunks(rows: Sequence[dict], size: int) -> Iterable[Sequence[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #


def upsert_instruments(session: Session, items: Iterable[InstrumentInfo]) -> int:
    """Insert/update the security master. Returns the number of rows written."""
    rows = []
    for item in items:
        if not item.code:
            continue
        rows.append(
            {
                "code": item.code,
                "name": item.name or "",
                "market": item.market or "",
                "board": item.board or "",
                "industry": "",
                "is_index": bool(item.is_index),
                "is_st": bool(item.is_st),
                "status": int(item.status),
                "list_date": item.list_date,
                "delist_date": item.delist_date,
                "source": item.source or "",
            }
        )
    if not rows:
        return 0

    written = 0
    for chunk in _chunks(rows, 500):
        stmt = sqlite_insert(Instrument).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[Instrument.code],
            set_={
                # Never overwrite a known name with a blank one.
                "name": func.coalesce(func.nullif(stmt.excluded.name, ""), Instrument.name),
                "market": stmt.excluded.market,
                "board": func.coalesce(func.nullif(stmt.excluded.board, ""), Instrument.board),
                "is_index": stmt.excluded.is_index,
                "status": stmt.excluded.status,
                "list_date": func.coalesce(stmt.excluded.list_date, Instrument.list_date),
                "delist_date": func.coalesce(stmt.excluded.delist_date, Instrument.delist_date),
                "source": stmt.excluded.source,
            },
        )
        session.execute(stmt)
        written += len(chunk)
    session.flush()
    return written


def upsert_calendar(session: Session, days: Iterable[date], source: str = "") -> int:
    """Mark ``days`` as open. Non-listed days stay absent (treated as closed)."""
    rows = [{"calendar_date": d, "is_open": True, "source": source} for d in days]
    if not rows:
        return 0
    written = 0
    for chunk in _chunks(rows, 900):
        # Values clauses are capped to stay well under SQLite's variable limit.
        stmt = sqlite_insert(TradeCalendar).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[TradeCalendar.calendar_date],
            set_={"is_open": stmt.excluded.is_open, "source": stmt.excluded.source},
        )
        session.execute(stmt)
        written += len(chunk)
    session.flush()
    return written


def upsert_index_constituents(
    session: Session, index_code: str, codes: Sequence[str], as_of: date, source: str = ""
) -> int:
    if not codes:
        return 0
    rows = [{"index_code": index_code, "code": c, "as_of": as_of, "source": source} for c in codes]
    written = 0
    for chunk in _chunks(rows, 500):
        stmt = sqlite_insert(IndexConstituent).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                IndexConstituent.index_code,
                IndexConstituent.code,
                IndexConstituent.as_of,
            ],
            set_={"source": stmt.excluded.source},
        )
        session.execute(stmt)
        written += len(chunk)
    session.flush()
    return written


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


def _bar_rows(code: str, bars: Sequence[Bar], adjust: str) -> list[dict]:
    """Build insert rows for one symbol/adjust pair.

    ``code`` is authoritative rather than ``bar.code``: the caller already
    normalised the symbol, so this keeps a stray provider-side spelling from
    writing rows under an unexpected key.
    """
    rows = []
    for b in bars:
        rows.append(
            {
                "code": code,
                "trade_date": b.trade_date,
                "adjust": adjust,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "preclose": b.preclose,
                "volume": float(b.volume or 0.0),
                "amount": float(b.amount or 0.0),
                "turn": b.turn,
                "pct_chg": b.pct_chg,
                "pe_ttm": b.pe_ttm,
                "pb_mrq": b.pb_mrq,
                "is_st": bool(b.is_st),
                "one_word_limit": False,
                "source": b.source or "",
            }
        )
    return rows


def upsert_daily_bars(
    session: Session,
    code: str,
    bars: Sequence[Bar],
    adjust: str,
    *,
    chunk_size: int = 400,
) -> int:
    """Upsert one symbol's bars for a given adjust mode (idempotent)."""
    if not bars:
        return 0
    rows = _bar_rows(code, bars, adjust)
    written = 0
    for chunk in _chunks(rows, chunk_size):
        stmt = sqlite_insert(DailyBar).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[DailyBar.code, DailyBar.trade_date, DailyBar.adjust],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                # Keep a known reference price if the new source omits it.
                "preclose": func.coalesce(stmt.excluded.preclose, DailyBar.preclose),
                "volume": stmt.excluded.volume,
                "amount": stmt.excluded.amount,
                "turn": func.coalesce(stmt.excluded.turn, DailyBar.turn),
                "pct_chg": func.coalesce(stmt.excluded.pct_chg, DailyBar.pct_chg),
                "pe_ttm": func.coalesce(stmt.excluded.pe_ttm, DailyBar.pe_ttm),
                "pb_mrq": func.coalesce(stmt.excluded.pb_mrq, DailyBar.pb_mrq),
                "is_st": stmt.excluded.is_st,
                "source": stmt.excluded.source,
            },
        )
        session.execute(stmt)
        written += len(chunk)
    session.flush()
    return written


def upsert_adjust_factors(session: Session, code: str, factors: Sequence[AdjustFactor]) -> int:
    if not factors:
        return 0
    rows = [
        {
            "code": code,
            "ex_date": f.ex_date,
            "qfq_factor": f.qfq_factor,
            "hfq_factor": f.hfq_factor,
            "cash_div": f.cash_div,
            "bonus_ratio": f.bonus_ratio,
            "source": f.source,
        }
        for f in factors
    ]
    written = 0
    for chunk in _chunks(rows, 400):
        stmt = sqlite_insert(AdjustFactor).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=[AdjustFactor.code, AdjustFactor.ex_date],
            set_={
                "qfq_factor": stmt.excluded.qfq_factor,
                "hfq_factor": stmt.excluded.hfq_factor,
                "cash_div": stmt.excluded.cash_div,
                "bonus_ratio": stmt.excluded.bonus_ratio,
                "source": stmt.excluded.source,
            },
        )
        session.execute(stmt)
        written += len(chunk)
    session.flush()
    return written


def delete_bars(
    session: Session,
    code: str,
    adjust: str,
    start: date | None = None,
    end: date | None = None,
) -> int:
    """Remove bars for a symbol/adjust, optionally within a date window.

    Used by the forward-adjustment rebuild: when a new ex-dividend date appears,
    the stored qfq series for that symbol is stale and must be replaced wholesale.
    """
    stmt = delete(DailyBar).where(DailyBar.code == code, DailyBar.adjust == adjust)
    if start is not None:
        stmt = stmt.where(DailyBar.trade_date >= start)
    if end is not None:
        stmt = stmt.where(DailyBar.trade_date <= end)
    result = session.execute(stmt)
    session.flush()
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def get_bars(
    session: Session,
    code: str,
    adjust: str = "raw",
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
) -> list[DailyBar]:
    """Bars for a symbol in ascending date order."""
    stmt = select(DailyBar).where(DailyBar.code == code, DailyBar.adjust == adjust)
    if start is not None:
        stmt = stmt.where(DailyBar.trade_date >= start)
    if end is not None:
        stmt = stmt.where(DailyBar.trade_date <= end)
    if limit is not None:
        # Take the most recent N, then re-sort ascending for callers.
        stmt = stmt.order_by(DailyBar.trade_date.desc()).limit(limit)
        rows = list(session.execute(stmt).scalars())
        rows.reverse()
        return rows
    stmt = stmt.order_by(DailyBar.trade_date.asc())
    return list(session.execute(stmt).scalars())


def last_bar_date(session: Session, code: str, adjust: str = "raw") -> date | None:
    stmt = select(func.max(DailyBar.trade_date)).where(DailyBar.code == code, DailyBar.adjust == adjust)
    return session.execute(stmt).scalar()


def bar_count(session: Session, code: str | None = None, adjust: str | None = None) -> int:
    stmt = select(func.count()).select_from(DailyBar)
    if code:
        stmt = stmt.where(DailyBar.code == code)
    if adjust:
        stmt = stmt.where(DailyBar.adjust == adjust)
    return int(session.execute(stmt).scalar() or 0)


def list_calendar(session: Session, start: date | None = None, end: date | None = None) -> list[date]:
    stmt = select(TradeCalendar.calendar_date).where(TradeCalendar.is_open.is_(True))
    if start is not None:
        stmt = stmt.where(TradeCalendar.calendar_date >= start)
    if end is not None:
        stmt = stmt.where(TradeCalendar.calendar_date <= end)
    return list(session.execute(stmt.order_by(TradeCalendar.calendar_date)).scalars())


def is_open_day(session: Session, day: date) -> bool:
    stmt = select(TradeCalendar.is_open).where(TradeCalendar.calendar_date == day)
    return bool(session.execute(stmt).scalar())


def get_instrument(session: Session, code: str) -> Instrument | None:
    return session.get(Instrument, code)


def list_instruments(
    session: Session,
    *,
    include_indices: bool = False,
    only_active: bool = True,
    query: str | None = None,
    limit: int | None = None,
) -> list[Instrument]:
    stmt = select(Instrument)
    if not include_indices:
        stmt = stmt.where(Instrument.is_index.is_(False))
    if only_active:
        stmt = stmt.where(Instrument.status == 1)
    if query:
        like = f"%{query}%"
        stmt = stmt.where((Instrument.code.like(like)) | (Instrument.name.like(like)))
    stmt = stmt.order_by(Instrument.code)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def get_adjust_factors(session: Session, code: str) -> list[AdjustFactor]:
    stmt = select(AdjustFactor).where(AdjustFactor.code == code).order_by(AdjustFactor.ex_date)
    return list(session.execute(stmt).scalars())


def index_members(session: Session, index_code: str) -> list[str]:
    """Members of an index as of the most recent snapshot on record."""
    latest = session.execute(
        select(func.max(IndexConstituent.as_of)).where(IndexConstituent.index_code == index_code)
    ).scalar()
    if latest is None:
        return []
    stmt = select(IndexConstituent.code).where(
        IndexConstituent.index_code == index_code,
        IndexConstituent.as_of == latest,
    )
    return list(session.execute(stmt).scalars())
