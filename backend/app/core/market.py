"""A-share market mechanics: price limits, sessions, T+1, lot sizes.

Centralised so the backtester, alert engine and API all agree on the rules.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.config import settings

# --- Sessions (Asia/Shanghai) ---
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

# One lot = 100 shares for A-shares.
LOT_SIZE = 100

# Board-aware daily price-limit ratios.
LIMIT_ST_MAIN = 0.05
LIMIT_MAIN = 0.10
LIMIT_STAR_CHINEXT = 0.20  # 科创板 / 创业板
LIMIT_BJ = 0.30


def price_limit_ratio(
    code: str,
    name: str = "",  # noqa: ARG001 - kept for call-site symmetry
    is_st: bool = False,
) -> float:
    """Daily price-limit ratio for an instrument.

    ST names on the main board are capped at 5%; STAR/ChiNext at 20%; BSE at 30%.
    """
    digits, exchange = code.split(".") if "." in code else (code, "")
    if digits.startswith(("688", "689")):  # 科创板
        return LIMIT_STAR_CHINEXT
    if digits.startswith(("300", "301")):  # 创业板
        return LIMIT_STAR_CHINEXT
    if exchange == "BJ" or digits.startswith(("43", "83", "87", "88", "920")):
        return LIMIT_BJ
    return LIMIT_ST_MAIN if is_st else LIMIT_MAIN


def limit_prices(preclose: float, ratio: float, decimals: int = 2) -> tuple[float, float]:
    """Return ``(limit_down, limit_up)`` rounded to the exchange's tick precision.

    Both A-share exchanges round the limit price to 2 decimals (half-up).
    """
    if preclose <= 0:
        return (0.0, 0.0)
    up = _half_up(preclose * (1.0 + ratio), decimals)
    down = _half_up(preclose * (1.0 - ratio), decimals)
    return (down, up)


def _half_up(value: float, decimals: int) -> float:
    from decimal import ROUND_HALF_UP, Decimal

    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def is_limit_up(close: float, preclose: float, code: str, name: str = "", is_st: bool = False) -> bool:
    """True when ``close`` sits at the upper limit (untradeable for buys)."""
    _, up = limit_prices(preclose, price_limit_ratio(code, name, is_st))
    return up > 0 and close >= up - 1e-6


def is_limit_down(close: float, preclose: float, code: str, name: str = "", is_st: bool = False) -> bool:
    """True when ``close`` sits at the lower limit (untradeable for sells)."""
    down, _ = limit_prices(preclose, price_limit_ratio(code, name, is_st))
    return down > 0 and close <= down + 1e-6


def is_one_word_limit(
    open_: float,  # noqa: ARG001 - kept for OHLC call-site symmetry
    high: float,
    low: float,
    preclose: float,
    code: str,
    name: str = "",
    is_st: bool = False,
) -> bool:
    """True for a 一字板: the whole session pinned at one limit price."""
    _, up = limit_prices(preclose, price_limit_ratio(code, name, is_st))
    down, _ = limit_prices(preclose, price_limit_ratio(code, name, is_st))
    if up <= 0:
        return False
    if abs(high - low) > 1e-6:
        return False
    return abs(high - up) <= 1e-6 or abs(low - down) <= 1e-6


def is_trading_time(now: datetime | None = None) -> bool:
    """Whether ``now`` falls inside a continuous-trading session (local tz)."""
    now = now or datetime.now(settings.tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=settings.tz)
    t = now.astimezone(settings.tz).time()
    return (MORNING_OPEN <= t <= MORNING_CLOSE) or (AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE)


def is_call_auction(now: datetime | None = None) -> bool:
    """09:15-09:25 opening auction and 14:57-15:00 closing auction."""
    now = now or datetime.now(settings.tz)
    t = now.astimezone(settings.tz).time()
    return (time(9, 15) <= t < time(9, 30)) or (time(14, 57) <= t <= AFTERNOON_CLOSE)


def is_tradable_session(now: datetime | None = None) -> bool:
    """Trading time, excluding the pre-open auction."""
    now = now or datetime.now(settings.tz)
    return is_trading_time(now) and not is_call_auction(now)


def next_trade_day_after(days: list[date], ref: date) -> date | None:
    """First open day strictly after ``ref`` from a sorted list of open days."""
    for d in days:
        if d > ref:
            return d
    return None


def prev_trade_day(days: list[date], ref: date) -> date | None:
    """Last open day strictly before ``ref``."""
    prev = None
    for d in days:
        if d < ref:
            prev = d
        else:
            break
    return prev


def add_trade_days(days: list[date], ref: date, n: int) -> date | None:
    """Shift ``ref`` by ``n`` trading days within a sorted open-day list."""
    if n == 0:
        return ref
    step = 1 if n > 0 else -1
    remaining = abs(n)
    cur = ref
    while remaining > 0:
        nxt = next_trade_day_after(days, cur) if step > 0 else prev_trade_day(days, cur)
        if nxt is None:
            return None
        cur = nxt
        remaining -= 1
    return cur


def lot_floor(shares: float) -> int:
    """Round a share count down to a whole lot."""
    if shares < LOT_SIZE:
        return 0
    return int(shares // LOT_SIZE) * LOT_SIZE


def approximate_trade_days(start: date, end: date) -> int:
    """Rough open-day count (~243/yr); for progress estimates only, never settlement."""
    if end <= start:
        return 0
    return max(1, int((end - start).days * 243 / 365))


def default_history_start(years: int = 5, today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=365 * years)
