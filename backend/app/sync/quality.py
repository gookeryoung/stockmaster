"""Data quality checks for the stored daily pipeline.

Why this exists
---------------
Every downstream number — indicators, factors, model probabilities, backtest
returns — is only as trustworthy as the bars underneath it. Silent data problems
(a symbol that stopped updating, a qfq series left on an old adjustment basis, a
gap where history was never fetched) do not raise errors; they simply produce
wrong analysis. This module turns those silent problems into an explicit report.

Checks implemented
------------------
``missing_history``   no bars at all
``missing_qfq``       only the raw series exists (analysis needs qfq)
``orphan_qfq``        qfq exists without raw, so a corporate action cannot be rebuilt
``short_history``     fewer bars than analysis needs
``sparse``            covers materially fewer sessions than the calendar
``gap``               a long hole between consecutive bars (calendar unavailable)
``stale``             newest bar lags the market's latest session
``ohlc_invalid``      non-positive prices or impossible OHLC ordering
``extreme_return``    move beyond the plausible price limit
``qfq_discontinuity`` a jump the ex-dates do not explain

Two levels of reporting
-----------------------
1. ``coverage`` — whole-market availability counts. This answers the question a
   user actually has ("how much of the market do I have?") and stays meaningful
   when only part of the market has been synced.
2. per-symbol ``findings`` — bounded detail for the symbols actually inspected,
   with dates and values so every claim can be audited.

Performance: per-symbol checks are SQL aggregates, and full rows are never loaded
for a symbol that has no data. Only offending rows come back for price checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.market import price_limit_ratio
from app.db.models import DailyBar, Instrument, TradeCalendar
from app.sync.adjust import find_adjustment_jumps

log = get_logger(__name__)

__all__ = [
    "DataQualityReport",
    "Finding",
    "SymbolQuality",
    "check_data_quality",
    "summarize_for_log",
]

#: Below this many bars a symbol cannot support the factor windows used in
#: training (the longest rolling window is 252 sessions).
MIN_BARS_FOR_ANALYSIS = 60

#: Expected share of calendar sessions. Suspensions are real, so this is lenient by
#: design — it flags genuine gaps, not legitimate trading halts.
MIN_CALENDAR_COVERAGE = 0.80

#: A daily move beyond this multiple of the board's price limit cannot be explained
#: by normal trading (limit 10% -> 12% tolerance for rounding and adjustments).
EXTREME_RETURN_TOLERANCE = 1.2

#: Max examples carried per symbol, bounding report size on the full market.
MAX_EXAMPLES = 5

#: Default cap on per-symbol detail. Whole-market coverage is always computed, so
#: this only limits how much detail is returned.
DEFAULT_MAX_SYMBOLS = 200

#: Gap allowance when no trading calendar is stored.
GAP_DAYS_WITHOUT_CALENDAR = 10


@dataclass
class Finding:
    """One concrete data problem."""

    kind: str
    severity: str  # info | warning | error
    message: str
    code: str | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class SymbolQuality:
    """Per-symbol assessment."""

    code: str
    name: str = ""
    bars: int = 0
    first_date: date | None = None
    last_date: date | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the symbol has no error-level finding."""
        return not any(f.severity == "error" for f in self.findings)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "bars": self.bars,
            "first_date": str(self.first_date) if self.first_date else None,
            "last_date": str(self.last_date) if self.last_date else None,
            "ok": self.ok,
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass
class DataQualityReport:
    """Aggregate result of a quality scan."""

    checked: int = 0
    symbols: list[SymbolQuality] = field(default_factory=list)
    global_findings: list[Finding] = field(default_factory=list)
    latest_market_date: date | None = None
    calendar_sessions: int = 0
    adjust: str = "qfq"
    #: Whole-market availability counts (see :func:`_market_coverage`).
    coverage: dict = field(default_factory=dict)

    def _count(self, severity: str) -> int:
        return sum(1 for s in self.symbols for f in s.findings if f.severity == severity) + sum(
            1 for f in self.global_findings if f.severity == severity
        )

    @property
    def error_count(self) -> int:
        return self._count("error")

    @property
    def warning_count(self) -> int:
        return self._count("warning")

    @property
    def healthy_symbols(self) -> int:
        return sum(1 for s in self.symbols if s.ok)

    @property
    def unhealthy_symbols(self) -> int:
        return self.checked - self.healthy_symbols

    def issue_counts(self) -> dict[str, int]:
        """How many symbols exhibit each issue kind."""
        counts: dict[str, int] = {}
        for symbol in self.symbols:
            for kind in {f.kind for f in symbol.findings}:
                counts[kind] = counts.get(kind, 0) + 1
        for finding in self.global_findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def as_dict(self, *, include_symbols: bool = True, only_problems: bool = False) -> dict:
        symbols = self.symbols
        if only_problems:
            symbols = [s for s in symbols if s.findings]
        payload: dict = {
            "adjust": self.adjust,
            "checked": self.checked,
            "healthy": self.healthy_symbols,
            "unhealthy": self.unhealthy_symbols,
            "errors": self.error_count,
            "warnings": self.warning_count,
            "latest_market_date": str(self.latest_market_date) if self.latest_market_date else None,
            "calendar_sessions": self.calendar_sessions,
            "coverage": self.coverage,
            "issue_counts": self.issue_counts(),
            "global_findings": [f.as_dict() for f in self.global_findings],
        }
        if include_symbols:
            payload["symbols"] = [s.as_dict() for s in symbols]
        return payload


def _calendar_open_days(session: Session, start: date, end: date) -> set[date]:
    """Open sessions from the stored calendar within ``[start, end]``."""
    rows = (
        session.execute(
            select(TradeCalendar.calendar_date).where(
                TradeCalendar.is_open.is_(True),
                TradeCalendar.calendar_date >= start,
                TradeCalendar.calendar_date <= end,
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


def _market_coverage(session: Session, adjust: str, latest_market_date: date, stale_days: int) -> dict:
    """Whole-market availability counts for the graded series.

    Reported independently of the bounded per-symbol scan, because "only 8 of 5576
    symbols have been synced" is the single most useful availability fact and it
    must not be hidden by a scan cap.
    """
    total_stocks = (
        session.execute(
            select(func.count()).select_from(Instrument).where(Instrument.is_index.is_(False), Instrument.status == 1)
        ).scalar()
        or 0
    )

    per_symbol = session.execute(
        select(DailyBar.code, func.count(), func.max(DailyBar.trade_date))
        .where(DailyBar.adjust == adjust)
        .group_by(DailyBar.code)
    ).all()

    with_data = len(per_symbol)
    usable = sum(1 for _code, bars, _last in per_symbol if bars >= MIN_BARS_FOR_ANALYSIS)
    cutoff = latest_market_date.toordinal() - stale_days
    current = sum(1 for _code, _bars, last in per_symbol if last and last.toordinal() >= cutoff)

    return {
        "stocks_total": total_stocks,
        "stocks_with_series": with_data,
        "stocks_usable": usable,
        "stocks_current": current,
        "stocks_without_series": max(0, total_stocks - with_data),
        "usable_share": round(usable / total_stocks, 4) if total_stocks else 0.0,
    }


def _pick_targets(session: Session, adjust: str, codes: list[str] | None, max_symbols: int) -> list[str]:
    """Symbols to grade in detail.

    Defaults to the **instrument master**, not to symbols that already have bars.
    Selecting on the presence of bars would make the scan structurally blind to its
    own headline failure: a symbol with no data is exactly what needs reporting, and
    it can never appear in a list derived from data.

    Ordering prefers recently updated symbols, so a bounded scan still covers the
    part of the market in active use. Pass ``codes`` for a full audit.
    """
    if codes:
        return list(dict.fromkeys(codes))

    latest_bar = (
        select(DailyBar.code, func.max(DailyBar.trade_date).label("last_date"))
        .where(DailyBar.adjust == adjust)
        .group_by(DailyBar.code)
        .subquery()
    )
    rows = session.execute(
        select(Instrument.code)
        .outerjoin(latest_bar, latest_bar.c.code == Instrument.code)
        .where(Instrument.is_index.is_(False), Instrument.status == 1)
        .order_by(latest_bar.c.last_date.desc().nullslast(), Instrument.code)
        .limit(max_symbols)
    ).scalars()
    return list(rows)


def check_data_quality(
    session: Session,
    *,
    codes: list[str] | None = None,
    adjust: str = "qfq",
    stale_days: int = 5,
    coverage_threshold: float = MIN_CALENDAR_COVERAGE,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
) -> DataQualityReport:
    """Scan stored bars for silent data problems.

    Args:
        session: database session.
        codes: restrict per-symbol detail; defaults to the most recently updated
            instruments. Pass an explicit list for a full audit of a universe.
        adjust: which stored series to grade (``qfq`` by default — that is what
            analysis and training consume).
        stale_days: calendar days of lag before a symbol counts as stale.
        coverage_threshold: minimum share of calendar sessions expected.
        max_symbols: cap on symbols inspected in detail, bounding report size.

    Returns:
        A :class:`DataQualityReport` with coverage, per-symbol and global findings.
    """
    report = DataQualityReport(adjust=adjust)

    latest_market_date = session.execute(
        select(func.max(DailyBar.trade_date)).where(DailyBar.adjust == adjust)
    ).scalar()
    report.latest_market_date = latest_market_date

    if latest_market_date is None:
        report.global_findings.append(
            Finding(
                kind="no_data",
                severity="error",
                message=f"数据库中没有 {adjust} 行情数据；请先运行数据同步。",
            )
        )
        report.coverage = _market_coverage(session, adjust, date.today(), stale_days)
        return report

    report.coverage = _market_coverage(session, adjust, latest_market_date, stale_days)

    if report.coverage.get("stocks_without_series"):
        report.global_findings.append(
            Finding(
                kind="incomplete_market",
                severity="info",
                message=(
                    f"{report.coverage['stocks_without_series']} / "
                    f"{report.coverage['stocks_total']} 只在市证券尚未同步 {adjust} 数据"
                    "（可执行 sync-daily --full 回补）。"
                ),
                detail=report.coverage,
            )
        )

    targets = _pick_targets(session, adjust, codes, max_symbols)
    report.checked = len(targets)
    if not targets:
        report.global_findings.append(
            Finding(
                kind="no_symbols",
                severity="warning",
                message="没有找到可检查的标的（证券主数据为空）。",
            )
        )
        return report

    # --- calendar coverage window -------------------------------------------
    earliest = session.execute(
        select(func.min(DailyBar.trade_date)).where(DailyBar.adjust == adjust, DailyBar.code.in_(targets))
    ).scalar()
    open_days = _calendar_open_days(session, earliest or latest_market_date, latest_market_date)
    report.calendar_sessions = len(open_days)
    if not open_days:
        report.global_findings.append(
            Finding(
                kind="no_calendar",
                severity="warning",
                message="交易日历为空，无法按日历计算覆盖率；请运行交易日历同步以启用该项检查。",
            )
        )

    names = dict(session.execute(select(Instrument.code, Instrument.name).where(Instrument.code.in_(targets))).all())

    for code in targets:
        report.symbols.append(
            _check_symbol(
                session,
                code=code,
                name=names.get(code, ""),
                adjust=adjust,
                latest_market_date=latest_market_date,
                open_days=open_days,
                stale_days=stale_days,
                coverage_threshold=coverage_threshold,
            )
        )

    report.global_findings.extend(_check_series_pairing(session, targets, adjust))
    return report


def _check_symbol(
    session: Session,
    *,
    code: str,
    name: str,
    adjust: str,
    latest_market_date: date,
    open_days: set[date],
    stale_days: int,
    coverage_threshold: float,
) -> SymbolQuality:
    """Grade one symbol using aggregates, fetching rows only when needed."""
    quality = SymbolQuality(code=code, name=name)

    first_date, last_date, bars = session.execute(
        select(
            func.min(DailyBar.trade_date),
            func.max(DailyBar.trade_date),
            func.count(),
        ).where(DailyBar.code == code, DailyBar.adjust == adjust)
    ).one()

    quality.bars = int(bars or 0)
    quality.first_date = first_date
    quality.last_date = last_date

    if quality.bars == 0:
        # Distinguish "nothing at all" from "only this series is missing": the
        # remedies differ (full sync vs. re-sync of one adjust mode).
        other = "raw" if adjust == "qfq" else "qfq"
        counterpart = (
            session.execute(
                select(func.count()).select_from(DailyBar).where(DailyBar.code == code, DailyBar.adjust == other)
            ).scalar()
            or 0
        )
        if counterpart:
            quality.findings.append(
                Finding(
                    kind=f"missing_{adjust}",
                    severity="error",
                    code=code,
                    message=(f"{code} 只有 {other} 序列（{counterpart} 根），缺少 {adjust} 序列，分析与训练无法使用。"),
                    detail={"missing": adjust, "counterpart_bars": counterpart},
                )
            )
        else:
            quality.findings.append(
                Finding(
                    kind="missing_history",
                    severity="error",
                    code=code,
                    message=f"{code} 没有任何行情数据。",
                )
            )
        return quality

    if quality.bars < MIN_BARS_FOR_ANALYSIS:
        quality.findings.append(
            Finding(
                kind="short_history",
                severity="warning",
                code=code,
                message=(f"{code} 仅有 {quality.bars} 根日线，少于分析所需的 {MIN_BARS_FOR_ANALYSIS} 根。"),
                detail={"bars": quality.bars, "required": MIN_BARS_FOR_ANALYSIS},
            )
        )

    # --- staleness ----------------------------------------------------------
    lag = (latest_market_date - last_date).days
    if lag > stale_days:
        quality.findings.append(
            Finding(
                kind="stale",
                severity="warning",
                code=code,
                message=(f"{code} 最新数据为 {last_date}，落后全市场 {lag} 天（可能已停牌或同步遗漏）。"),
                detail={"last_date": str(last_date), "lag_days": lag},
            )
        )

    # --- coverage / gaps ----------------------------------------------------
    if open_days:
        sessions_in_range = {d for d in open_days if first_date <= d <= last_date}
        if sessions_in_range:
            stored = (
                session.execute(
                    select(func.count(func.distinct(DailyBar.trade_date))).where(
                        DailyBar.code == code,
                        DailyBar.adjust == adjust,
                        DailyBar.trade_date.in_(sessions_in_range),
                    )
                ).scalar()
                or 0
            )
            coverage = stored / len(sessions_in_range)
            if coverage < coverage_threshold:
                quality.findings.append(
                    Finding(
                        kind="sparse",
                        severity="warning",
                        code=code,
                        message=(f"{code} 在其区间内仅覆盖 {coverage:.1%} 的交易日（阈值 {coverage_threshold:.0%}）。"),
                        detail={
                            "coverage": round(coverage, 4),
                            "sessions": len(sessions_in_range),
                            "stored": stored,
                        },
                    )
                )
    else:
        gap = _find_largest_gap(session, code, adjust)
        if gap:
            quality.findings.append(
                Finding(
                    kind="gap",
                    severity="warning",
                    code=code,
                    message=(f"{code} 在 {gap['from']} 与 {gap['to']} 之间存在 {gap['days']} 天缺口。"),
                    detail=gap,
                )
            )

    # --- OHLC validity and extreme returns ---------------------------------
    invalid = _find_invalid_ohlc(session, code, adjust)
    if invalid:
        quality.findings.append(
            Finding(
                kind="ohlc_invalid",
                severity="error",
                code=code,
                message=f"{code} 有价格不合法或 OHLC 顺序错误的日线。",
                detail={"examples": invalid},
            )
        )

    limit = price_limit_ratio(code)
    extremes = _find_extreme_returns(session, code, adjust, limit)
    if extremes:
        quality.findings.append(
            Finding(
                kind="extreme_return",
                severity="warning",
                code=code,
                message=(
                    f"{code} 有 {len(extremes)} 个交易日涨跌幅超出该板块限制"
                    f"（±{limit:.0%}）的合理范围，前复权基准可能不一致。"
                ),
                detail={"examples": extremes[:MAX_EXAMPLES]},
            )
        )

    # --- adjustment continuity (qfq only) ----------------------------------
    if adjust == "qfq":
        jumps = _find_qfq_discontinuities(session, code, limit)
        if jumps:
            quality.findings.append(
                Finding(
                    kind="qfq_discontinuity",
                    severity="error",
                    code=code,
                    message=(f"{code} 前复权序列出现 {len(jumps)} 处异常跳变，通常意味着复权基准不一致（需要重建）。"),
                    detail={"examples": jumps[:MAX_EXAMPLES]},
                )
            )

    return quality


def _find_invalid_ohlc(session: Session, code: str, adjust: str) -> list[dict]:
    """Rows whose prices are non-positive or whose OHLC ordering is impossible."""
    rows = session.execute(
        select(DailyBar.trade_date, DailyBar.open, DailyBar.high, DailyBar.low, DailyBar.close)
        .where(
            DailyBar.code == code,
            DailyBar.adjust == adjust,
            or_(
                DailyBar.open <= 0,
                DailyBar.high <= 0,
                DailyBar.low <= 0,
                DailyBar.close <= 0,
                DailyBar.high < DailyBar.low,
                DailyBar.open > DailyBar.high,
                DailyBar.open < DailyBar.low,
                DailyBar.close > DailyBar.high,
                DailyBar.close < DailyBar.low,
            ),
        )
        .order_by(DailyBar.trade_date)
        .limit(MAX_EXAMPLES)
    ).all()

    out: list[dict] = []
    for trade_date, open_, high, low, close in rows:
        reason = "non-positive price" if min(open_, high, low, close) <= 0 else "OHLC out of order"
        out.append(
            {
                "date": str(trade_date),
                "reason": reason,
                "values": [open_, high, low, close],
            }
        )
    return out


def _find_extreme_returns(session: Session, code: str, adjust: str, limit: float) -> list[dict]:
    """Day-over-day moves beyond the board's plausible price limit.

    Uses a window function so only offending rows come back, rather than pulling
    the whole series into Python.
    """
    previous = func.lag(DailyBar.close).over(order_by=DailyBar.trade_date)
    subquery = (
        select(
            DailyBar.trade_date.label("trade_date"),
            DailyBar.close.label("close"),
            previous.label("previous_close"),
        )
        .where(DailyBar.code == code, DailyBar.adjust == adjust)
        .subquery()
    )

    rows = session.execute(
        select(subquery.c.trade_date, subquery.c.close, subquery.c.previous_close)
        .where(
            subquery.c.previous_close.isnot(None),
            subquery.c.previous_close > 0,
            func.abs(subquery.c.close / subquery.c.previous_close - 1.0) > limit * EXTREME_RETURN_TOLERANCE,
        )
        .order_by(subquery.c.trade_date)
    ).all()

    return [
        {
            "date": str(trade_date),
            "change_pct": round((close / previous_close - 1.0) * 100, 2),
        }
        for trade_date, close, previous_close in rows
    ]


def _find_qfq_discontinuities(session: Session, code: str, limit: float) -> list[dict]:
    """Jumps in the qfq series that no ex-date explains.

    Needs neighbouring values, so only the close series is loaded — and this runs
    for a bounded set of symbols.
    """
    rows = session.execute(
        select(DailyBar.trade_date, DailyBar.close)
        .where(DailyBar.code == code, DailyBar.adjust == "qfq")
        .order_by(DailyBar.trade_date)
    ).all()
    if len(rows) < 3:
        return []

    series = dict(rows)
    return [
        {"date": str(day), "change_pct": round(change * 100, 2)}
        for day, change in find_adjustment_jumps(series, threshold=max(0.25, limit * 1.5))
    ]


def _find_largest_gap(session: Session, code: str, adjust: str) -> dict | None:
    """Largest spacing between consecutive bars, via a window function."""
    previous = func.lag(DailyBar.trade_date).over(order_by=DailyBar.trade_date)
    subquery = (
        select(DailyBar.trade_date.label("trade_date"), previous.label("previous_date"))
        .where(DailyBar.code == code, DailyBar.adjust == adjust)
        .subquery()
    )
    row = session.execute(
        select(subquery.c.previous_date, subquery.c.trade_date)
        .where(subquery.c.previous_date.isnot(None))
        .order_by((func.julianday(subquery.c.trade_date) - func.julianday(subquery.c.previous_date)).desc())
        .limit(1)
    ).first()

    if not row or not row[0] or not row[1]:
        return None
    # SQLite can hand back dates as strings through a window function, so
    # normalise before doing date arithmetic.
    previous_date = _as_date(row[0])
    current_date = _as_date(row[1])
    days = (current_date - previous_date).days
    if days <= GAP_DAYS_WITHOUT_CALENDAR:
        return None
    return {"from": str(previous_date), "to": str(current_date), "days": days}


def _as_date(value) -> date:
    """Coerce a SQLite date/datetime/string into a ``date``."""
    if hasattr(value, "hour"):  # datetime
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _check_series_pairing(session: Session, codes: list[str], adjust: str) -> list[Finding]:
    """Detect rows present in one adjust series but not the other.

    A qfq series without a raw counterpart cannot be rebuilt after a corporate
    action — precisely when correctness matters most.
    """
    other = "raw" if adjust == "qfq" else "qfq"

    current_codes = set(
        session.execute(
            select(DailyBar.code).where(DailyBar.adjust == adjust, DailyBar.code.in_(codes)).group_by(DailyBar.code)
        ).scalars()
    )
    other_codes = set(
        session.execute(
            select(DailyBar.code).where(DailyBar.adjust == other, DailyBar.code.in_(codes)).group_by(DailyBar.code)
        ).scalars()
    )

    findings: list[Finding] = []

    missing = sorted(current_codes - other_codes)
    if missing:
        findings.append(
            Finding(
                kind=f"orphan_{adjust}",
                severity="error",
                message=(
                    f"{len(missing)} 只标的只有 {adjust} 序列、缺少 {other} 序列，"
                    f"复权重建将无法进行。示例：{', '.join(missing[:5])}"
                ),
                detail={"examples": missing[:MAX_EXAMPLES], "missing": other},
            )
        )

    only_other = sorted(other_codes - current_codes)
    if only_other:
        findings.append(
            Finding(
                kind=f"missing_{adjust}",
                severity="error",
                message=(
                    f"{len(only_other)} 只标的缺少 {adjust} 序列（仅有 {other}），"
                    f"分析与训练无法使用。示例：{', '.join(only_other[:5])}"
                ),
                detail={"examples": only_other[:MAX_EXAMPLES], "missing": adjust},
            )
        )

    return findings


def summarize_for_log(report: DataQualityReport) -> str:
    """One-line summary, for logs and job output."""
    coverage = report.coverage or {}
    return (
        f"data-quality: checked={report.checked} healthy={report.healthy_symbols} "
        f"unhealthy={report.unhealthy_symbols} errors={report.error_count} "
        f"warnings={report.warning_count} "
        f"usable={coverage.get('stocks_usable', '?')}/{coverage.get('stocks_total', '?')} "
        f"issues={report.issue_counts()}"
    )
