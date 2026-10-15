"""Forward/backward price adjustment (复权).

The correctness problem
----------------------
Forward-adjusted (前复权) prices are *retroactive*: when a stock goes ex-dividend
today, every historical qfq price changes. A sync that blindly appends or upserts
new qfq rows therefore splices a newly-scaled series onto an old one, producing a
fake jump at the boundary — which silently corrupts any backtest or indicator
computed across it.

Strategy
--------
``raw`` (不复权) is the immutable base. The qfq series is treated as derived
output:

1. Track the set of ex-dates seen per symbol in ``adjust_factors``.
2. After each sync, if the ex-date set changed, the symbol's qfq series is
   **rebuilt from raw** rather than appended to.
3. Sanity-check the rebuilt series for adjustment-boundary continuity.

Two independent mechanisms are provided:

* :func:`derive_qfq_from_ratio` — uses a cumulative factor series. Baostock does
  not expose a factor table directly, so the ratio is *recovered* empirically
  from the provider's own raw vs qfq closes (:func:`recover_adjust_ratio`). This
  keeps us consistent with the source's own convention, including cash dividends.
* :func:`apply_adjustment` — applies a cash-dividend/ bonus-share formula, used
  when explicit corporate-action data is available (``query_dividend_data``).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class RatioPoint:
    """A step change in the adjustment ratio, effective from ``ex_date``."""

    ex_date: date
    #: Multiplier applied to raw prices at and after ``ex_date`` (backward adj),
    #: or its inverse for forward adjustment. See ``recover_adjust_ratio``.
    ratio: float


def recover_adjust_ratio(
    raw_prices: dict[date, float], qfq_prices: dict[date, float], *, tolerance: float = 1e-6
) -> list[RatioPoint]:
    """Recover the step-wise qfq/raw ratio implied by a provider's own output.

    This exploits a structural property: between two consecutive ex-dividend
    dates the qfq/raw ratio is *constant*, and it steps at each ex-date. Dividing
    the two series therefore yields exactly the discrete factor schedule, with no
    need to model dividends or share bonuses explicitly.

    Returns points sorted ascending, omitting the trivial ``1.0`` steps.
    """
    common = sorted(set(raw_prices) & set(qfq_prices))
    points: list[RatioPoint] = []
    previous_ratio: float | None = None

    for day in common:
        raw = raw_prices[day]
        qfq = qfq_prices[day]
        if raw <= 0:
            continue
        ratio = qfq / raw
        if previous_ratio is None or abs(ratio - previous_ratio) > tolerance:
            points.append(RatioPoint(ex_date=day, ratio=ratio))
            previous_ratio = ratio
    return points


def derive_qfq_from_ratio(raw: dict[date, float], points: list[RatioPoint]) -> dict[date, float]:
    """Scale raw closes into qfq closes using recovered ratio steps.

    ``points`` must be ascending; each point applies from its date onward, with
    earlier dates using the earliest known ratio (the "most recent factor"
    convention that 前复权 uses for the displayed series).
    """
    if not points:
        return dict(raw)
    out: dict[date, float] = {}
    ordered = sorted(points, key=lambda p: p.ex_date)
    for day, price in raw.items():
        ratio = ordered[0].ratio
        for point in ordered:
            if point.ex_date <= day:
                ratio = point.ratio
            else:
                break
        out[day] = price * ratio
    return out


def apply_adjustment(
    prices: dict[date, float],
    actions: list[tuple[date, float, float]],
    *,
    forward: bool = True,
) -> dict[date, float]:
    """Apply corporate actions to a price series.

    Args:
        prices: raw closes keyed by date (ascending assumed).
        actions: ``(ex_date, cash_div_per_share, bonus_ratio)`` tuples, where
            ``bonus_ratio`` is new-shares-per-held-share (0.2 = 10送2).
        forward: ``True`` produces 前复权, ``False`` produces 后复权.

    The ex-date factor needed to map *post-ex* raw prices onto the pre-ex basis is::

        f = (1 + bonus_ratio) + cash_div / P_pre

    where ``P_pre`` is the raw price used as reference. A cash dividend lowers the
    post-ex price, so the pre-ex price is worth more; 前复权 (``forward=True``)
    therefore *divides* historical prices by every factor that applies later in
    time, keeping the most recent price at its raw value::

        qfq(t) = raw(t) / prod(f_i for ex_date_i > t)

    后复权 (``forward=False``) *multiplies* by every factor from the past, so the
    earliest price keeps its raw value::

        hfq(t) = raw(t) * prod(f_i for ex_date_i <= t)

    For a pure bonus the two conventions differ only by an overall constant, which
    is why both are usable for backtesting while their anchor dates differ.
    """
    if not actions:
        return dict(prices)
    ordered = sorted(actions, key=lambda a: a[0])
    out: dict[date, float] = {}

    for day, price in prices.items():
        factor = 1.0
        for ex_date, cash, bonus in ordered:
            step = (1.0 + bonus) + (cash / price if price > 0 else 0.0)
            if (forward and ex_date > day) or (not forward and ex_date <= day):
                factor *= step
        if forward:
            out[day] = price / factor if factor else price
        else:
            out[day] = price * factor
    return out


def find_adjustment_jumps(series: dict[date, float], *, threshold: float = 0.25) -> list[tuple[date, float]]:
    """Detect suspicious day-over-day jumps, for continuity validation.

    A correctly adjusted series should never jump more than the price limit on a
    normal day. A jump far beyond it usually means the series was spliced from
    two differently-scaled segments — exactly the bug this module exists to
    prevent. Returns ``(date, return)`` pairs exceeding ``threshold``.
    """
    days = sorted(series)
    jumps: list[tuple[date, float]] = []
    for prev_day, day in itertools.pairwise(days):
        prev = series[prev_day]
        if prev <= 0:
            continue
        ret = series[day] / prev - 1.0
        if abs(ret) > threshold:
            jumps.append((day, ret))
    return jumps


def is_continuity_broken(series: dict[date, float], *, threshold: float = 0.25) -> bool:
    """Whether the series contains an implausible adjustment-boundary jump."""
    return bool(find_adjustment_jumps(series, threshold=threshold))


def ex_date_set(points: list[RatioPoint]) -> set[date]:
    """Ex-dates implied by recovered ratio steps (ignoring the first anchor)."""
    if not points:
        return set()
    return {p.ex_date for p in points[1:]}
