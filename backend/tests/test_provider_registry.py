"""Registry failover and circuit-breaker tests (offline)."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.errors import ProviderError, ProviderUnavailable
from app.providers.mock_provider import FailingProvider, MockProvider, synthetic_bars
from app.providers.registry import ProviderRegistry


@pytest.fixture
def window():
    return date(2026, 1, 1), date(2026, 3, 31)


class TestFailover:
    def test_primary_serves_when_healthy(self, window):
        primary = MockProvider(name="primary")
        backup = MockProvider(name="backup")
        reg = ProviderRegistry([primary, backup], order=["primary", "backup"])
        bars, source = reg.fetch_daily_with_source("600519.SH", *window)
        assert source == "primary"
        assert bars and all(b.source == "primary" for b in bars)

    def test_falls_back_to_second_when_first_fails(self, window):
        bad = FailingProvider(name="bad")
        good = MockProvider(name="good")
        reg = ProviderRegistry([bad, good], order=["bad", "good"])
        bars, source = reg.fetch_daily_with_source("600519.SH", *window)
        assert source == "good"
        assert bars
        # The failing provider was attempted exactly once for this call.
        assert bad.calls == 1

    def test_circuit_opens_after_threshold_and_skips(self, window, monkeypatch):
        monkeypatch.setenv("SM_PROVIDER_CIRCUIT_THRESHOLD", "3")
        monkeypatch.setenv("SM_PROVIDER_CIRCUIT_COOLDOWN", "3600")
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            bad = FailingProvider(name="bad")
            good = MockProvider(name="good")
            reg = ProviderRegistry([bad, good], order=["bad", "good"])

            # Each call attempts `bad` once, then fails over to `good`.
            for _ in range(3):
                reg.fetch_daily("600519.SH", *window)
            assert bad.calls == 3

            states = {s["name"]: s for s in reg.states()}
            assert states["bad"]["circuit_open"] is True

            # While the breaker is open, `bad` must not be attempted again.
            reg.fetch_daily("600519.SH", *window)
            assert bad.calls == 3

            # And it is excluded from the available order.
            assert "bad" not in reg.available_order()
        finally:
            get_settings.cache_clear()

    def test_success_resets_failure_counter(self, window):
        reg = ProviderRegistry([MockProvider(name="ok")], order=["ok"])
        state = reg._states["ok"]
        state.record_failure("boom")
        assert state.consecutive_failures == 1
        reg.fetch_daily("600519.SH", *window)
        assert state.consecutive_failures == 0
        assert state.opened_at is None

    def test_all_failing_raises_provider_unavailable(self, window):
        reg = ProviderRegistry(
            [FailingProvider(name="a"), FailingProvider(name="b")],
            order=["a", "b"],
        )
        with pytest.raises(ProviderUnavailable) as exc:
            reg.fetch_daily("600519.SH", *window)
        assert "a:" in str(exc.value) and "b:" in str(exc.value)

    def test_unsupported_method_fails_over_without_counting_as_error(self, window):
        class NoSnapshots(MockProvider):
            name = "nosnap"

            def fetch_snapshot(self, codes=None):
                raise NotImplementedError("no snapshots here")

        reg = ProviderRegistry(
            [NoSnapshots(), MockProvider(name="snapper")],
            order=["nosnap", "snapper"],
        )
        snaps = reg.fetch_snapshot(["600519.SH"])
        assert snaps and snaps[0].source == "snapper"
        # NotImplementedError is a capability gap, not a health failure.
        assert reg._states["nosnap"].failures == 0
        assert reg._states["nosnap"].consecutive_failures == 0

    def test_bad_result_type_is_treated_as_failure(self, window):
        class WrongType(MockProvider):
            name = "wrong"

            def fetch_daily(self, code, start, end, adjust="raw"):
                return "not-a-list"

        reg = ProviderRegistry([WrongType(), MockProvider(name="right")], order=["wrong", "right"])
        bars = reg.fetch_daily("600519.SH", *window)
        assert isinstance(bars, list) and bars
        assert reg._states["wrong"].failures == 1

    def test_unconfigured_provider_is_appended_to_order(self):
        reg = ProviderRegistry([MockProvider(name="a"), MockProvider(name="b")], order=["a"])
        assert reg.order == ["a", "b"]

    def test_close_is_idempotent(self):
        reg = ProviderRegistry([MockProvider(name="a")], order=["a"])
        reg.close()
        reg.close()


class TestMockProvider:
    def test_synthetic_bars_are_weekdays_only(self):
        bars = synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 1, 31))
        assert bars
        assert all(b.trade_date.weekday() < 5 for b in bars)
        assert bars == sorted(bars, key=lambda b: b.trade_date)

    def test_synthetic_bars_are_deterministic(self):
        a = synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 2, 1))
        b = synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 2, 1))
        assert [x.close for x in a] == [x.close for x in b]

    def test_ohlc_ordering_is_valid(self):
        bars = synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 6, 30))
        for bar in bars:
            assert bar.low <= bar.open <= bar.high
            assert bar.low <= bar.close <= bar.high

    def test_scale_applied_to_all_price_fields(self):
        p = MockProvider(adjust_scale=2.0)
        bars = p.fetch_daily("600519.SH", date(2026, 1, 1), date(2026, 1, 10))
        base = synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 1, 10))
        assert bars[0].close == pytest.approx(base[0].close * 2)

    def test_calendar_matches_weekdays(self):
        p = MockProvider()
        days = p.fetch_calendar(date(2026, 1, 1), date(2026, 1, 31))
        expected = list(synthetic_bars("600519.SH", date(2026, 1, 1), date(2026, 1, 31)))
        assert days == [b.trade_date for b in expected]
        assert all(d.weekday() < 5 for d in days)

    def test_snapshot_filters_by_code(self):
        p = MockProvider()
        snaps = p.fetch_snapshot(["600519.SH"])
        assert len(snaps) == 1
        assert snaps[0].code == "600519.SH"


class TestFailingProvider:
    def test_counts_calls(self, window):
        p = FailingProvider()
        with pytest.raises(ProviderError):
            p.fetch_daily("600519.SH", *window)
        assert p.calls == 1
        assert p.health_check() is False
