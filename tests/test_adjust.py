"""Adjustment (复权) correctness tests.

These guard the highest-impact silent-corruption risk in the project: a
forward-adjusted series that was appended to instead of rebuilt when a new ex-date
appeared, producing a fake jump that would poison indicators and backtests.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stockmaster.sync.adjust import (
    RatioPoint,
    apply_adjustment,
    derive_qfq_from_ratio,
    ex_date_set,
    find_adjustment_jumps,
    is_continuity_broken,
    recover_adjust_ratio,
)


def _series(start: date, n: int, price: float = 100.0, drift: float = 1.0):
    return {start + timedelta(days=i): price * (drift**i) for i in range(n)}


class TestRecoverRatio:
    def test_constant_ratio_yields_single_point(self):
        raw = _series(date(2026, 1, 1), 10)
        qfq = {d: p * 0.5 for d, p in raw.items()}
        points = recover_adjust_ratio(raw, qfq)
        assert len(points) == 1
        assert points[0].ratio == pytest.approx(0.5)

    def test_step_is_detected_exactly_once(self):
        raw = _series(date(2026, 1, 1), 10)
        step_date = date(2026, 1, 5)
        qfq = {d: p * (0.9 if d >= step_date else 1.0) for d, p in raw.items()}
        points = recover_adjust_ratio(raw, qfq)
        assert len(points) == 2
        assert points[0].ratio == pytest.approx(1.0)
        assert points[1].ex_date == step_date
        assert points[1].ratio == pytest.approx(0.9)

    def test_multiple_steps(self):
        raw = _series(date(2026, 1, 1), 20)
        qfq = {}
        for d, p in raw.items():
            scale = 1.0
            if d >= date(2026, 1, 5):
                scale = 0.9
            if d >= date(2026, 1, 12):
                scale = 0.8
            qfq[d] = p * scale
        points = recover_adjust_ratio(raw, qfq)
        assert len(points) == 3
        assert [p.ratio for p in points] == pytest.approx([1.0, 0.9, 0.8])

    def test_ignores_non_overlapping_dates(self):
        raw = _series(date(2026, 1, 1), 5)
        qfq = _series(date(2026, 3, 1), 5)
        assert recover_adjust_ratio(raw, qfq) == []

    def test_ex_date_set_skips_anchor(self):
        raw = _series(date(2026, 1, 1), 10)
        qfq = {d: p * (0.9 if d >= date(2026, 1, 5) else 1.0) for d, p in raw.items()}
        points = recover_adjust_ratio(raw, qfq)
        assert ex_date_set(points) == {date(2026, 1, 5)}


class TestDeriveQfq:
    def test_apply_ratio_forward_from_date(self):
        raw = {date(2026, 1, 1): 100.0, date(2026, 1, 6): 100.0}
        points = [
            RatioPoint(date(2026, 1, 1), 1.0),
            RatioPoint(date(2026, 1, 5), 0.5),
        ]
        out = derive_qfq_from_ratio(raw, points)
        assert out[date(2026, 1, 1)] == pytest.approx(100.0)
        assert out[date(2026, 1, 6)] == pytest.approx(50.0)

    def test_round_trip_reproduces_source_series(self):
        """The derived qfq must match what the provider itself returned."""
        raw = _series(date(2026, 1, 1), 30, drift=1.002)
        points = [
            RatioPoint(date(2026, 1, 1), 1.0),
            RatioPoint(date(2026, 1, 10), 0.95),
            RatioPoint(date(2026, 1, 20), 0.90),
        ]

        def scale(day):
            s = 1.0
            for p in points:
                if p.ex_date <= day:
                    s = p.ratio
            return s

        provider_qfq = {d: p * scale(d) for d, p in raw.items()}
        recovered = recover_adjust_ratio(raw, provider_qfq)
        derived = derive_qfq_from_ratio(raw, recovered)
        for day in raw:
            assert derived[day] == pytest.approx(provider_qfq[day], rel=1e-9)

    def test_empty_points_returns_raw(self):
        raw = _series(date(2026, 1, 1), 3)
        assert derive_qfq_from_ratio(raw, []) == raw

    def test_dates_before_first_point_use_first_ratio(self):
        raw = {date(2026, 1, 1): 100.0, date(2026, 1, 10): 100.0}
        points = [RatioPoint(date(2026, 1, 5), 0.5)]
        out = derive_qfq_from_ratio(raw, points)
        # 2026-01-01 precedes every step, so the earliest ratio applies.
        assert out[date(2026, 1, 1)] == pytest.approx(50.0)


class TestApplyAdjustment:
    def test_forward_keeps_latest_price_at_raw_level(self):
        """前复权 anchors the *most recent* price, adjusting history behind it."""
        prices = {date(2026, 1, 1): 10.0, date(2026, 6, 1): 5.0}
        actions = [(date(2026, 3, 1), 0.0, 1.0)]  # 10送10
        out = apply_adjustment(prices, actions, forward=True)
        assert out[date(2026, 6, 1)] == pytest.approx(5.0)
        # Pre-ex price is halved because the share count doubled.
        assert out[date(2026, 1, 1)] == pytest.approx(5.0)

    def test_backward_keeps_earliest_price_at_raw_level(self):
        """后复权 anchors the listing price, scaling later prices up."""
        prices = {date(2026, 1, 1): 10.0, date(2026, 6, 1): 5.0}
        actions = [(date(2026, 3, 1), 0.0, 1.0)]  # 10送10
        out = apply_adjustment(prices, actions, forward=False)
        assert out[date(2026, 1, 1)] == pytest.approx(10.0)
        # Post-ex price is doubled to sit on the pre-ex basis.
        assert out[date(2026, 6, 1)] == pytest.approx(10.0)

    def test_forward_and_backward_differ_by_constant_bonus_factor(self):
        prices = _series(date(2026, 1, 1), 12, drift=1.0)
        actions = [(date(2026, 1, 6), 0.0, 0.5)]
        fwd = apply_adjustment(prices, actions, forward=True)
        bwd = apply_adjustment(prices, actions, forward=False)
        # For a pure bonus the two series are proportional (1+bonus apart).
        for day in prices:
            assert bwd[day] == pytest.approx(fwd[day] * 1.5, rel=1e-9)

    def test_cash_dividend_lowers_pre_ex_forward_price(self):
        prices = {date(2026, 1, 1): 10.0, date(2026, 6, 1): 9.0}
        actions = [(date(2026, 3, 1), 1.0, 0.0)]  # 1 yuan cash dividend
        out = apply_adjustment(prices, actions, forward=True)
        # Post-ex price is the anchor and stays raw.
        assert out[date(2026, 6, 1)] == pytest.approx(9.0)
        # Pre-ex price is scaled down by the dividend factor (1 + 1/9).
        assert out[date(2026, 1, 1)] == pytest.approx(10.0 / (1.0 + 1.0 / 10.0))

    def test_no_actions_is_identity(self):
        prices = _series(date(2026, 1, 1), 4)
        assert apply_adjustment(prices, []) == prices


class TestContinuity:
    def test_clean_series_has_no_jumps(self):
        raw = _series(date(2026, 1, 1), 30, drift=1.005)
        points = [RatioPoint(date(2026, 1, 1), 1.0), RatioPoint(date(2026, 1, 15), 0.9)]
        qfq = derive_qfq_from_ratio(raw, points)
        # A correct ratio application is continuous through the ex-date.
        assert find_adjustment_jumps(qfq, threshold=0.25) == []
        assert not is_continuity_broken(qfq)

    def test_spliced_series_is_detected(self):
        """Simulate the bug: old un-scaled rows + newly-scaled rows appended."""
        raw = _series(date(2026, 1, 1), 20, drift=1.0)
        spliced = {}
        for i, (d, p) in enumerate(sorted(raw.items())):
            scale = 1.0 if i < 10 else 0.5  # naive append without rebuild
            spliced[d] = p * scale
        jumps = find_adjustment_jumps(spliced, threshold=0.25)
        assert jumps, "a 50% boundary jump must be reported"
        assert is_continuity_broken(spliced)

    def test_ordinary_price_limit_move_is_not_flagged(self):
        # A legitimate 10% move must not be mistaken for a broken series.
        series = {date(2026, 1, 1): 100.0, date(2026, 1, 2): 110.0}
        assert find_adjustment_jumps(series, threshold=0.25) == []

    def test_suspended_gap_is_tolerated(self):
        series = {date(2026, 1, 1): 100.0, date(2026, 6, 1): 105.0}
        assert find_adjustment_jumps(series, threshold=0.25) == []

    def test_zero_previous_price_is_skipped(self):
        series = {date(2026, 1, 1): 0.0, date(2026, 1, 2): 100.0}
        assert find_adjustment_jumps(series) == []
