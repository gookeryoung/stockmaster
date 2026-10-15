"""Tencent (腾讯) provider — first fallback.

Backs onto ``akshare.stock_zh_a_hist_tx``, which serves adjusted daily bars over
Tencent's quote endpoints. This source is *not* affected by the Eastmoney block
described in the project findings, so it is the natural backup for baostock.

Known gaps vs. the primary source: no ``preclose``, no ``isST``, no valuation
fields. ``turnover`` is a free-float turnover fraction (not percent), so it is
stored as a fraction and consumers must not assume percent.
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.errors import DataError, ProviderError
from app.core.logging import get_logger
from app.core.symbols import normalize, to_tencent
from app.providers.base import (
    ADJUST_HFQ,
    ADJUST_QFQ,
    ADJUST_RAW,
    Bar,
    BaseProvider,
    InstrumentInfo,
)

log = get_logger(__name__)

# Tencent uses sh/sz/bj prefixed symbols; qfq/hfq are supported, 'none' is raw.
_ADJUST_ARG = {ADJUST_RAW: "", ADJUST_QFQ: "qfq", ADJUST_HFQ: "hfq"}

_INDEX_PREFIXES = ("sh000", "sz399", "sh950", "csi")


class TencentProvider(BaseProvider):
    """Adjusted daily bars via Tencent."""

    name = "tencent"

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    @staticmethod
    def _ak():
        import akshare as ak

        return ak

    @staticmethod
    def _parse_date(value) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()

    @staticmethod
    def _f(value) -> float | None:
        try:
            if value is None:
                return None
            out = float(value)
        except (TypeError, ValueError):
            return None
        # pandas may hand back NaN.
        return None if out != out else out  # noqa: PLR0124 - NaN check

    def fetch_daily(
        self,
        code: str,
        start: date,
        end: date,
        adjust: str = ADJUST_RAW,
    ) -> list[Bar]:
        if adjust not in _ADJUST_ARG:
            raise DataError(f"unsupported adjust mode: {adjust!r}")
        canon = normalize(code)
        symbol = to_tencent(canon)
        ak = self._ak()
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=_ADJUST_ARG[adjust],
            )
        except Exception as exc:
            raise ProviderError(f"tencent daily {symbol} failed: {exc}") from exc

        if df is None or len(df) == 0:
            return []

        bars: list[Bar] = []
        for row in df.to_dict("records"):
            try:
                day = self._parse_date(row.get("date"))
                o = self._f(row.get("open"))
                h = self._f(row.get("high"))
                lo = self._f(row.get("low"))
                c = self._f(row.get("close"))
            except Exception:
                continue
            if None in (o, h, lo, c):
                continue
            bars.append(
                Bar(
                    code=canon,
                    trade_date=day,
                    open=o,
                    high=h,
                    low=lo,
                    close=c,
                    volume=self._f(row.get("volume")) or 0.0,
                    amount=self._f(row.get("amount")) or 0.0,
                    turn=self._f(row.get("turnover")),
                    adjust=adjust,
                    source=self.name,
                )
            )
        bars.sort(key=lambda b: b.trade_date)
        return bars

    def fetch_index_daily(self, code: str, start: date, end: date) -> list[Bar]:
        """Tencent index bars via akshare's index endpoint."""
        canon = normalize(code)
        symbol = to_tencent(canon)
        ak = self._ak()
        try:
            df = ak.stock_zh_index_daily_tx(symbol=symbol)
        except Exception as exc:
            raise ProviderError(f"tencent index {symbol} failed: {exc}") from exc
        if df is None or len(df) == 0:
            return []
        bars: list[Bar] = []
        for row in df.to_dict("records"):
            day = self._parse_date(row.get("date"))
            if day < start or day > end:
                continue
            o, c = self._f(row.get("open")), self._f(row.get("close"))
            if o is None or c is None:
                continue
            bars.append(
                Bar(
                    code=canon,
                    trade_date=day,
                    open=o,
                    high=self._f(row.get("high")) or o,
                    low=self._f(row.get("low")) or c,
                    close=c,
                    volume=self._f(row.get("volume")) or 0.0,
                    amount=self._f(row.get("amount")) or 0.0,
                    adjust=ADJUST_RAW,
                    source=self.name,
                )
            )
        bars.sort(key=lambda b: b.trade_date)
        return bars

    def fetch_calendar(self, start: date, end: date) -> list[date]:
        """Trade calendar from Sina's full history (Tencent has no calendar API)."""
        ak = self._ak()
        try:
            df = ak.tool_trade_date_hist_sina()
        except Exception as exc:
            raise ProviderError(f"calendar fetch failed: {exc}") from exc
        if df is None or len(df) == 0:
            return []
        days = []
        for value in df["trade_date"].tolist():
            day = self._parse_date(value)
            if start <= day <= end:
                days.append(day)
        days.sort()
        return days

    def list_instruments(self) -> list[InstrumentInfo]:
        ak = self._ak()
        try:
            df = ak.stock_info_a_code_name()
        except Exception as exc:
            raise ProviderError(f"instrument list failed: {exc}") from exc
        if df is None or len(df) == 0:
            return []
        out: list[InstrumentInfo] = []
        for row in df.to_dict("records"):
            raw_code = str(row.get("code", "")).strip()
            if len(raw_code) != 6 or not raw_code.isdigit():
                continue
            try:
                canon = normalize(raw_code)
            except DataError:
                continue
            _digits, exchange = canon.split(".")
            out.append(
                InstrumentInfo(
                    code=canon,
                    name=str(row.get("name", "")).strip(),
                    market=exchange,
                    board="",
                    is_index=False,
                    source=self.name,
                )
            )
        return out

    def health_check(self) -> bool:
        try:
            end = date.today()
            bars = self.fetch_daily("600519.SH", end, end, ADJUST_QFQ)
            return isinstance(bars, list)
        except Exception as exc:
            log.debug("tencent health check failed: %s", exc)
            return False
