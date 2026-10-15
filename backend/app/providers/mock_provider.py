"""Offline test doubles for the provider layer.

These let the whole sync/indicator/backtest chain run deterministically without
network access or provider rate limits — important because the real sources
throttle and occasionally hang.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.core.errors import ProviderError
from app.core.symbols import normalize
from app.providers.base import (
    ADJUST_RAW,
    Bar,
    BaseProvider,
    InstrumentInfo,
    Snapshot,
)


def synthesize_params(code: str) -> tuple[float, float]:
    """Derive a symbol-specific (start_price, daily_return) pair from the code.

    Symbols must **not** share one series: cross-sectional factors standardise
    values *across* symbols on a given date, so identical series give zero
    dispersion, undefined z-scores and no composite score. A naive hash-modulo
    scheme collides easily (e.g. ``000001.SZ`` and ``601318.SH``), which silently
    reintroduces that problem, so the digit sum is spread over two moduli and the
    price/return pairs use coprime-ish steps.
    """
    digits = [ord(c) for c in code]
    h = sum(digits)
    h2 = sum(d * (i + 3) for i, d in enumerate(digits))

    start_price = 8.0 + (h % 47) * 4.3
    daily_return = -0.003 + (h2 % 29) * 0.00028
    return start_price, daily_return


def synthetic_bars(
    code: str,
    start: date,
    end: date,
    *,
    start_price: float | None = None,
    daily_return: float | None = None,
    adjust: str = ADJUST_RAW,
    source: str = "mock",
) -> list[Bar]:
    """Deterministic geometric price series over calendar days.

    Only weekdays are emitted, which exercises weekday/date logic without
    pretending to be a real trading calendar. Price level and drift are derived
    from ``code`` unless overridden, so different symbols produce different series
    (see :func:`synthesize_params`).
    """
    derived_price, derived_return = synthesize_params(normalize(code))
    if start_price is None:
        start_price = derived_price
    if daily_return is None:
        daily_return = derived_return

    bars: list[Bar] = []
    price = start_price
    day = start
    prev_close = start_price
    while day <= end:
        if day.weekday() < 5:  # Mon-Fri
            # Small deterministic wiggle so indicators are not perfectly linear.
            wiggle = ((day.toordinal() % 7) - 3) * 0.002
            close = max(0.01, price * (1 + daily_return + wiggle))
            high = max(price, close) * 1.01
            low = min(price, close) * 0.99
            bars.append(
                Bar(
                    code=normalize(code),
                    trade_date=day,
                    open=price,
                    high=high,
                    low=low,
                    close=close,
                    volume=1_000_000.0,
                    amount=close * 1_000_000.0,
                    preclose=prev_close,
                    pct_chg=(close / prev_close - 1) * 100 if prev_close else None,
                    turn=0.01,
                    is_st=False,
                    adjust=adjust,
                    source=source,
                )
            )
            prev_close = close
            price = close
        day += timedelta(days=1)
    return bars


class MockProvider(BaseProvider):
    """Always-succeeding provider backed by synthetic data."""

    name = "mock"

    def __init__(self, *, adjust_scale: float = 1.0, name: str | None = None) -> None:
        if name:
            self.name = name
        self._scale = adjust_scale

    def fetch_daily(self, code, start, end, adjust=ADJUST_RAW):
        bars = synthetic_bars(code, start, end, adjust=adjust, source=self.name)
        if self._scale != 1.0:
            for b in bars:
                b.open *= self._scale
                b.high *= self._scale
                b.low *= self._scale
                b.close *= self._scale
        return bars

    def fetch_calendar(self, start, end):
        return [d for d in _daterange(start, end) if d.weekday() < 5]

    def list_instruments(self):
        return [
            InstrumentInfo(code="600519.SH", name="Mock A", market="SH", board="main", source=self.name),
            InstrumentInfo(code="000001.SZ", name="Mock B", market="SZ", board="main", source=self.name),
        ]

    def fetch_snapshot(self, codes=None):
        wanted = [normalize(c) for c in codes] if codes else ["600519.SH", "000001.SZ"]
        return [
            Snapshot(code=c, name=f"Mock {c}", price=100.0, preclose=99.0, pct_chg=1.01, source=self.name)
            for c in wanted
        ]


class FailingProvider(BaseProvider):
    """Always-failing provider, for exercising failover and circuit breaking."""

    def __init__(self, *, name: str = "failing", exc: type[Exception] = ProviderError) -> None:
        self.name = name
        self._exc = exc
        self.calls = 0

    def _boom(self):
        self.calls += 1
        raise self._exc(f"{self.name} deliberately unavailable")

    def fetch_daily(self, code, start, end, adjust=ADJUST_RAW):
        return self._boom()

    def fetch_calendar(self, start, end):
        return self._boom()

    def list_instruments(self):
        return self._boom()

    def fetch_snapshot(self, codes=None):
        return self._boom()

    def health_check(self) -> bool:
        return False


class HangingProvider(BaseProvider):
    """Blocks forever on fetch, for exercising timeout handling."""

    name = "hanging"

    def __init__(self, *, name: str = "hanging") -> None:
        self.name = name

    def fetch_daily(self, code, start, end, adjust=ADJUST_RAW):
        import time

        time.sleep(3600)
        return []

    def fetch_calendar(self, start, end):
        import time

        time.sleep(3600)
        return []

    def list_instruments(self):
        import time

        time.sleep(3600)
        return []


def _daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
