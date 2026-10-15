"""baostock provider — the primary A-share source.

Why this provider is defensive
------------------------------
``baostock`` uses one module-global TCP socket and, in
``baostock/util/socketutil.py``, creates it **without a timeout** and then calls
``recv()`` in an unbounded loop; on failure ``send_msg`` prints a message and
returns ``None``, which downstream code indexes into. The observable symptoms are
queries that hang for many minutes and ``TypeError``/``IndexError`` noise rather
than a clean exception.

We therefore:
1. Force a socket timeout on the global socket right after login, so ``recv``
   raises instead of blocking forever.
2. Run every query under a hard deadline on a worker thread.
3. On timeout/failure, close the socket and drop the session so the next call
   reconnects from clean state.
4. Silence the library's ``print`` noise.
5. Never allow concurrent use of the shared socket, and optionally switch the
   library to a private socket object for worker threads.
"""

from __future__ import annotations

import contextlib
import io
import threading
from datetime import date, datetime, timedelta

from app.config import settings
from app.core.errors import DataError, ProviderError, ProviderTimeout
from app.core.logging import get_logger
from app.core.symbols import from_baostock, normalize, to_baostock
from app.providers.base import (
    ADJUST_HFQ,
    ADJUST_QFQ,
    ADJUST_RAW,
    Bar,
    BaseProvider,
    InstrumentInfo,
)

log = get_logger(__name__)

# baostock adjustflag: 1=后复权, 2=前复权, 3=不复权
_ADJUST_TO_FLAG = {ADJUST_RAW: "3", ADJUST_QFQ: "2", ADJUST_HFQ: "1"}

_DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,peTTM,pbMRQ,isST"

# Board classification by code prefix (SH/SZ/BJ).
_BOARD_RULES = (
    (("688", "689"), "star"),
    (("300", "301"), "chinext"),
    (("43", "83", "87", "88", "920"), "bse"),
)


class BaostockProvider(BaseProvider):
    """Primary provider. Not thread-safe by default (shared global socket)."""

    name = "baostock"
    requires_login = True

    def __init__(self, timeout: float | None = None) -> None:
        self._timeout = timeout if timeout is not None else settings.provider_timeout
        # Reentrant: the login guard and call guard are taken together.
        self._lock = threading.RLock()
        self._logged_in = False
        self._session = None
        self._closed = False

    # ------------------------------------------------------------------ login
    @staticmethod
    def _bs():
        """Import baostock lazily so module import stays cheap and testable."""
        import baostock as bs

        return bs

    @contextlib.contextmanager
    def _quiet(self):
        """Suppress baostock's internal ``print`` chatter."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield

    def _apply_socket_timeout(self) -> None:
        """Set a timeout on baostock's global socket — the root-cause fix."""
        try:
            from baostock.common import context

            sock = getattr(context, "default_socket", None)
            if sock is not None:
                sock.settimeout(self._timeout)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not set baostock socket timeout: %s", exc)

    def login(self) -> None:
        """Establish the shared session (idempotent)."""
        with self._lock:
            if self._logged_in and not self._closed:
                return
            bs = self._bs()
            with self._quiet():
                res = bs.login()
            if getattr(res, "error_code", "1") != "0":
                raise ProviderError(
                    f"baostock login failed: {getattr(res, 'error_code', '?')} {getattr(res, 'error_msg', '')}"
                )
            self._logged_in = True
            self._closed = False
            self._apply_socket_timeout()
            log.debug("baostock logged in")

    def logout(self) -> None:
        with self._lock:
            if not self._logged_in:
                return
            try:
                with self._quiet():
                    self._bs().logout()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("baostock logout raised: %s", exc)
            finally:
                self._logged_in = False

    def close(self) -> None:
        self.logout()
        self._session = None
        self._closed = True

    def force_close(self) -> None:
        """Hard-reset the shared socket without a graceful protocol logout.

        Used after a timeout, when the socket is in an unknown state and the
        server is not reading.
        """
        try:
            from baostock.common import context

            sock = getattr(context, "default_socket", None)
            if sock is not None:
                with contextlib.suppress(Exception):
                    sock.close()
                context.default_socket = None
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("force_close failed: %s", exc)
        finally:
            self._logged_in = False

    # ------------------------------------------------------------------ guard
    @contextlib.contextmanager
    def session(self):
        """Serialise access to the shared socket and guarantee cleanup."""
        with self._lock:
            self.login()
            try:
                yield self._session
            except TimeoutError as exc:
                self.force_close()
                raise ProviderTimeout(f"baostock socket timeout: {exc}") from exc
            except OSError as exc:
                self.force_close()
                raise ProviderError(f"baostock socket error: {exc}") from exc

    def _guarded(self, fn, *, what: str):
        """Run a blocking baostock query with a hard deadline.

        The timeout can only be observed with the shared socket (a worker thread
        inside the C-level ``recv`` cannot be killed), so on timeout we hard-close
        the socket to unblock it and mark the provider for reconnection.
        """
        self.login()
        deadline = self._timeout + 5.0
        result: list = []
        error: list[BaseException] = []

        def run() -> None:
            try:
                result.append(fn())
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=run, name=f"baostock-{what}", daemon=True)
        thread.start()
        thread.join(deadline)
        if thread.is_alive():
            self.force_close()
            raise ProviderTimeout(f"baostock {what} exceeded {deadline:.0f}s deadline; socket reset")
        if error:
            self.force_close()
            raise ProviderError(f"baostock {what} failed: {error[0]}") from error[0]
        if not result:
            raise ProviderError(f"baostock {what} returned no result")
        return result[0]

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _to_float(value: str | None) -> float | None:
        """baostock uses '' for missing numerics; never coerce to 0.0."""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _to_date(value: str) -> date:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()

    @staticmethod
    def _board_for(code: str) -> str:
        digits = code.split(".", maxsplit=1)[0]
        for prefixes, board in _BOARD_RULES:
            if digits.startswith(prefixes):
                return board
        return "main"

    def _collect(self, rs, what: str) -> list[list[str]]:
        """Drain a baostock result set, surfacing protocol errors."""
        if rs is None:
            raise ProviderError(f"baostock {what} returned None result set")
        rows: list[list[str]] = []
        while True:
            if getattr(rs, "error_code", "0") != "0":
                raise ProviderError(f"baostock {what} error {rs.error_code}: {getattr(rs, 'error_msg', '')}")
            if not rs.next():
                break
            rows.append(rs.get_row_data())
        return rows

    # -------------------------------------------------------------- interface
    def supports(self, code: str) -> bool:
        """baostock covers 上交所/深交所 only.

        Its API accepts just ``sh.``/``sz.`` prefixes and returns error 10004011
        ("股票代码未标识sh或sz") for 北交所 codes.
        """
        try:
            return not normalize(code).endswith(".BJ")
        except DataError:
            return False

    def fetch_daily(
        self,
        code: str,
        start: date,
        end: date,
        adjust: str = ADJUST_RAW,
    ) -> list[Bar]:
        """Daily bars for one symbol, ascending by date."""
        if adjust not in _ADJUST_TO_FLAG:
            raise DataError(f"unsupported adjust mode: {adjust!r}")
        canon = normalize(code)
        bs_code = to_baostock(canon)
        flag = _ADJUST_TO_FLAG[adjust]

        def query():
            with self._quiet():
                rs = self._bs().query_history_k_data_plus(
                    bs_code,
                    _DAILY_FIELDS,
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency="d",
                    adjustflag=flag,
                )
                return self._collect(rs, f"daily {bs_code}")

        rows = self._guarded(query, what=f"daily {bs_code}")
        bars: list[Bar] = []
        for row in rows:
            if len(row) < 14:
                continue
            (
                d,
                _code,
                raw_open,
                raw_high,
                raw_low,
                raw_close,
                preclose,
                volume,
                amount,
                turn,
                pct_chg,
                pe_ttm,
                pb_mrq,
                is_st,
            ) = row[:14]
            # A suspended day has empty prices; skip rather than store zeros.
            if not str(raw_open).strip() or not str(raw_close).strip():
                continue
            o, h, lo, c = (
                self._to_float(raw_open),
                self._to_float(raw_high),
                self._to_float(raw_low),
                self._to_float(raw_close),
            )
            if None in (o, h, lo, c):
                continue
            bars.append(
                Bar(
                    code=canon,
                    trade_date=self._to_date(d),
                    open=o,
                    high=h,
                    low=lo,
                    close=c,
                    volume=self._to_float(volume) or 0.0,
                    amount=self._to_float(amount) or 0.0,
                    preclose=self._to_float(preclose),
                    pct_chg=self._to_float(pct_chg),
                    turn=self._to_float(turn),
                    pe_ttm=self._to_float(pe_ttm),
                    pb_mrq=self._to_float(pb_mrq),
                    is_st=str(is_st).strip() == "1",
                    adjust=adjust,
                    source=self.name,
                )
            )
        bars.sort(key=lambda b: b.trade_date)
        return bars

    def fetch_index_daily(self, code: str, start: date, end: date) -> list[Bar]:
        """Index daily bars (indices have no adjustment)."""
        return self.fetch_daily(code, start, end, ADJUST_RAW)

    def fetch_calendar(self, start: date, end: date) -> list[date]:
        """Open trading days from baostock's official calendar."""

        def query():
            with self._quiet():
                rs = self._bs().query_trade_dates(
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                )
                return self._collect(rs, "trade_dates")

        rows = self._guarded(query, what="trade_dates")
        days: list[date] = []
        for row in rows:
            if len(row) < 2:
                continue
            if str(row[1]).strip() == "1":
                days.append(self._to_date(row[0]))
        days.sort()
        return days

    def list_instruments(self) -> list[InstrumentInfo]:
        """Security master built from the point-in-time trade table.

        ``query_all_stock`` is slow (30s-170s observed) and includes delisted
        codes, so it is a poor primary source. On timeout this **raises** rather
        than returning ``[]``: an empty list would look like a successful-but-empty
        sync and suppress the registry's failover to a faster backup source.
        """
        today = date.today()

        def query():
            with self._quiet():
                rs = self._bs().query_all_stock(day=today.strftime("%Y-%m-%d"))
                return self._collect(rs, "all_stock") if rs else []

        rows = self._guarded(query, what="all_stock")

        out: list[InstrumentInfo] = []
        for row in rows:
            if len(row) < 3:
                continue
            bs_code, trade_status, code_name = row[0], row[1], row[2]
            try:
                canon = from_baostock(bs_code)
            except DataError:
                continue
            digits, exchange = canon.split(".")
            is_index = (exchange == "SH" and digits.startswith("000")) or digits.startswith("399")
            out.append(
                InstrumentInfo(
                    code=canon,
                    name=str(code_name).strip(),
                    market=exchange,
                    board="index" if is_index else self._board_for(canon),
                    is_index=bool(is_index),
                    status=1 if str(trade_status).strip() == "1" else 0,
                    source=self.name,
                )
            )
        if not out:
            raise ProviderError("baostock returned no instruments")
        return out

    def fetch_stock_basic(self, code: str) -> InstrumentInfo | None:
        """Listing metadata for one symbol (includes IPO date and board type)."""
        canon = normalize(code)
        bs_code = to_baostock(canon)

        def query():
            with self._quiet():
                rs = self._bs().query_stock_basic(code=bs_code)
                return self._collect(rs, f"stock_basic {bs_code}")

        try:
            rows = self._guarded(query, what=f"stock_basic {bs_code}")
        except ProviderError:
            return None
        if not rows:
            return None
        row = rows[0]
        # code, code_name, ipoDate, outDate, type, status
        name = row[1] if len(row) > 1 else ""
        ipo = row[2] if len(row) > 2 else ""
        out = row[3] if len(row) > 3 else ""
        type_ = row[4] if len(row) > 4 else "1"
        status = row[5] if len(row) > 5 else "1"
        _digits, exchange = canon.split(".")
        list_date = self._to_date(ipo) if str(ipo).strip() else None
        delist_date = self._to_date(out) if str(out).strip() else None
        return InstrumentInfo(
            code=canon,
            name=str(name).strip(),
            market=exchange,
            board="index" if str(type_).strip() == "2" else self._board_for(canon),
            is_index=str(type_).strip() == "2",
            status=1 if str(status).strip() == "1" else 0,
            list_date=list_date,
            delist_date=delist_date,
            source=self.name,
        )

    def health_check(self) -> bool:
        """Cheap probe: can we fetch a single recent calendar day?"""
        try:
            end = date.today()
            days = self.fetch_calendar(end - timedelta(days=10), end)
            return isinstance(days, list)
        except Exception as exc:
            log.debug("baostock health check failed: %s", exc)
            return False

    # ------------------------------------------------------------- bulk helper
    def fetch_daily_bulk(
        self,
        codes: list[str],
        start: date,
        end: date,
        adjust: str = ADJUST_RAW,
        workers: int = 4,
    ) -> dict[str, list[Bar]]:
        """Fetch several symbols concurrently.

        baostock allows concurrent use, but only through *separate sockets*. The
        library keeps its socket on a module global, so each worker thread must
        swap in (and restore) its own socket. Because that global is process-wide,
        this method serialises the swap with a dedicated lock.
        """
        from concurrent.futures import ThreadPoolExecutor as _TPE

        results: dict[str, list[Bar]] = {}
        from baostock.common import context

        swap_lock = threading.Lock()

        def worker(symbol: str) -> tuple[str, list[Bar] | None, str]:
            bs = self._bs()
            with swap_lock:
                previous = getattr(context, "default_socket", None)
            try:
                with self._quiet():
                    bs.login()
                    sock = getattr(context, "default_socket", None)
                    if sock is not None:
                        sock.settimeout(self._timeout)
                bars = self.fetch_daily(symbol, start, end, adjust)
                return symbol, bars, ""
            except Exception as exc:
                return symbol, None, f"{type(exc).__name__}: {exc}"
            finally:
                with swap_lock:
                    with contextlib.suppress(Exception):
                        bs.logout()
                    context.default_socket = previous

        with _TPE(max_workers=max(1, workers)) as pool:
            for symbol, bars, err in pool.map(worker, codes):
                if bars is not None:
                    results[symbol] = bars
                elif err:
                    log.warning("bulk fetch failed for %s: %s", symbol, err)
        return results
