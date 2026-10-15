"""Sina (新浪) provider — second fallback and the intraday snapshot source.

Backs onto Sina's quote endpoints via akshare. Two things are notable:

1. Daily bars come from ``stock_zh_a_daily`` with English column names; Sina
   additionally exposes ``outstanding_share``, which makes turnover derivable.
2. The market-wide snapshot ``stock_zh_a_spot`` returns **Chinese** column names.
   Its encoding depends on the runtime locale, so columns are mapped
   *positionally* against the documented 14-column layout instead of by name.
   That keeps the parser robust when the console/DB encoding differs.
"""

from __future__ import annotations

from datetime import date, datetime

from app.core.errors import DataError, ProviderError
from app.core.logging import get_logger
from app.core.symbols import normalize, to_sina
from app.providers.base import (
    ADJUST_HFQ,
    ADJUST_QFQ,
    ADJUST_RAW,
    Bar,
    BaseProvider,
    InstrumentInfo,
    Snapshot,
)

log = get_logger(__name__)

# Sina's adjust argument: qfq/hfq map directly; raw is expressed as "" or none.
_ADJUST_ARG = {ADJUST_RAW: "", ADJUST_QFQ: "qfq", ADJUST_HFQ: "hfq"}

# Documented positional layout of stock_zh_a_spot:
# 代码 名称 最新价 涨跌额 涨跌幅 买入 卖出 昨收 今开 最高 最低 成交量 成交额 时间戳
_SPOT_FIELDS = (
    "code", "name", "price", "change", "pct_chg", "bid", "ask",
    "preclose", "open", "high", "low", "volume", "amount", "quote_time",
)


class SinaProvider(BaseProvider):
    """Daily bars plus market-wide snapshots."""

    name = "sina"

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    @staticmethod
    def _ak():
        import akshare as ak  # noqa: PLC0415

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
        return None if out != out else out  # noqa: PLR0124

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
        symbol = to_sina(canon)
        ak = self._ak()
        try:
            df = ak.stock_zh_a_daily(
                symbol=symbol,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=_ADJUST_ARG[adjust],
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"sina daily {symbol} failed: {exc}") from exc

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
            except Exception:  # noqa: BLE001
                continue
            if None in (o, h, lo, c):
                continue
            # Sina's `turnover` is already a fraction of outstanding shares.
            bars.append(
                Bar(
                    code=canon,
                    trade_date=day,
                    open=o, high=h, low=lo, close=c,
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
        """Index bars via Sina (long history, e.g. 6000+ rows for CSI300)."""
        canon = normalize(code)
        symbol = to_sina(canon)
        ak = self._ak()
        try:
            df = ak.stock_zh_index_daily(symbol=symbol)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"sina index {symbol} failed: {exc}") from exc
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
        ak = self._ak()
        try:
            df = ak.tool_trade_date_hist_sina()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"sina calendar failed: {exc}") from exc
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
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"sina instrument list failed: {exc}") from exc
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

    # ------------------------------------------------------------- snapshots
    def fetch_snapshot(self, codes: list[str] | None = None) -> list[Snapshot]:
        """Market-wide snapshot; ``codes`` filters the result when supplied."""
        ak = self._ak()
        try:
            df = ak.stock_zh_a_spot()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"sina snapshot failed: {exc}") from exc
        if df is None or len(df) == 0:
            return []

        wanted: set[str] | None = None
        if codes:
            wanted = {normalize(c) for c in codes}

        raw_columns = list(df.columns)
        # Prefer English headers when present; otherwise fall back to position.
        if "code" in raw_columns or "symbol" in raw_columns:
            records = df.to_dict("records")
            use_positional = False
        else:
            records = [list(r) for r in df.itertuples(index=False, name=None)]
            use_positional = True

        out: list[Snapshot] = []
        for record in records:
            if use_positional:
                if len(record) < len(_SPOT_FIELDS):
                    continue
                row = dict(zip(_SPOT_FIELDS, record, strict=False))
            else:
                row = record
                if "symbol" in row and "code" not in row:
                    row["code"] = row["symbol"]

            raw_code = str(row.get("code", "")).strip().lower()
            if not raw_code:
                continue
            try:
                canon = normalize(raw_code)
            except DataError:
                continue
            if wanted is not None and canon not in wanted:
                continue
            out.append(
                Snapshot(
                    code=canon,
                    name=str(row.get("name", "")).strip(),
                    price=self._f(row.get("price") or row.get("最新价")),
                    preclose=self._f(row.get("preclose") or row.get("昨收")),
                    open=self._f(row.get("open") or row.get("今开")),
                    high=self._f(row.get("high") or row.get("最高")),
                    low=self._f(row.get("low") or row.get("最低")),
                    volume=self._f(row.get("volume") or row.get("成交量")),
                    amount=self._f(row.get("amount") or row.get("成交额")),
                    pct_chg=self._f(row.get("pct_chg") or row.get("涨跌幅")),
                    turnover=self._f(row.get("turnover") or row.get("换手率")),
                    quote_time=str(row.get("quote_time") or row.get("时间戳") or ""),
                    source=self.name,
                )
            )
        return out

    def health_check(self) -> bool:
        try:
            end = date.today()
            bars = self.fetch_daily("600519.SH", end, end, ADJUST_QFQ)
            return isinstance(bars, list)
        except Exception as exc:  # noqa: BLE001
            log.debug("sina health check failed: %s", exc)
            return False
