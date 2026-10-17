"""Tests for code-range accessibility boundaries.

Derived from directly probing every A-share code range against each provider:

| range              | baostock | tencent | sina |
|--------------------|----------|---------|------|
| 沪深主板/创业板/科创 | yes      | yes     | yes  |
| B股 (900/200)      | no       | ?       | yes  |
| 北交所 920xxx      | no       | no      | yes  |
| 北交所 43x/83x/87x | no       | no      | no   |
| 指数 sh/sz         | yes      | yes     | yes  |

Two behaviours are pinned:

1. **A capability gap is not a failure.** Declaring boundaries keeps unsupported
   symbols from incrementing the failure counter, which previously tripped the
   circuit breaker and then starved symbols the same provider *could* serve.
2. **Indices must be routed to the index endpoint** — the equity endpoint fails for
   index codes on the fallback providers.
"""

from __future__ import annotations

from datetime import date

import pytest

from stockmaster.core.errors import ProviderError, ProviderUnavailable
from stockmaster.providers.base import Bar, BaseProvider
from stockmaster.providers.registry import ProviderRegistry

WINDOW = (date(2026, 9, 1), date(2026, 10, 15))


class RecordingProvider(BaseProvider):
    """Succeeds for supported codes; records whether it was asked at all."""

    name = "recording"

    def __init__(self, *, supported_prefixes: tuple[str, ...] = ()) -> None:
        self.supported_prefixes = supported_prefixes
        self.calls: list[str] = []

    def supports(self, code: str) -> bool:
        return code.endswith(self.supported_prefixes)

    def fetch_daily(self, code, start, end, adjust="raw"):
        self.calls.append(code)
        return [Bar(code=code, trade_date=start, open=1.0, high=1.0, low=1.0, close=1.0, adjust=adjust)]

    def fetch_index_daily(self, code, start, end):
        self.calls.append(f"index:{code}")
        return [Bar(code=code, trade_date=start, open=2.0, high=2.0, low=2.0, close=2.0, adjust="raw")]

    def fetch_calendar(self, start, end):
        return [start]

    def list_instruments(self):
        return []


class FailingProvider(BaseProvider):
    """Never succeeds and counts how often it was asked."""

    def __init__(self, *, name: str = "failing") -> None:
        self.name = name
        self.calls: list[str] = []

    def fetch_daily(self, code, start, end, adjust="raw"):
        self.calls.append(code)
        raise ProviderError(f"{self.name} cannot serve {code}")

    def fetch_index_daily(self, code, start, end):
        self.calls.append(f"index:{code}")
        raise ProviderError(f"{self.name} cannot serve index {code}")

    def fetch_calendar(self, start, end):
        raise ProviderError("no calendar")

    def list_instruments(self):
        return []


class TestCapabilityGapIsNotFailure:
    def test_unsupported_symbol_is_skipped_without_counting(self):
        """A provider that declines a symbol must not accrue failures."""
        # Supports Shenzhen only; asked for a Beijing symbol.
        provider = RecordingProvider(supported_prefixes=(".SZ",))
        reg = ProviderRegistry([provider], order=["recording"])

        with pytest.raises(ProviderUnavailable):
            reg.fetch_daily("430047.BJ", *WINDOW)

        assert provider.calls == [], "provider was called despite not supporting the symbol"
        state = reg.states()[0]
        assert state["failures"] == 0
        assert state["circuit_open"] is False

    def test_unsupported_symbol_falls_through_to_a_supporting_provider(self):
        """The chain must skip a declining provider and still serve the symbol."""
        declining = RecordingProvider(supported_prefixes=(".SZ",))
        declining.name = "declining"
        supporting = RecordingProvider(supported_prefixes=(".BJ",))
        supporting.name = "supporting"

        reg = ProviderRegistry([declining, supporting], order=["declining", "supporting"])
        bars, source = reg.fetch_daily_with_source("920000.BJ", *WINDOW, adjust="raw")

        assert source == "supporting"
        assert bars
        assert declining.calls == []
        # The declining provider is still healthy.
        states = {s["name"]: s for s in reg.states()}
        assert states["declining"]["failures"] == 0
        assert states["declining"]["circuit_open"] is False

    def test_real_failure_still_counts(self):
        """Genuine failures must still be recorded — the skip must be narrow."""
        provider = FailingProvider(name="broken")
        reg = ProviderRegistry([provider], order=["broken"])

        with pytest.raises(ProviderUnavailable):
            reg.fetch_daily("600519.SH", *WINDOW)

        assert provider.calls == ["600519.SH"]
        assert reg.states()[0]["failures"] == 1


class TestIndexRouting:
    def test_index_uses_the_index_endpoint(self):
        provider = RecordingProvider(supported_prefixes=(".SH",))
        reg = ProviderRegistry([provider], order=["recording"])

        bars, source = reg.fetch_index_daily_with_source("000300.SH", *WINDOW)

        assert source == "recording"
        assert provider.calls == ["index:000300.SH"], provider.calls
        assert bars and bars[0].close == 2.0

    def test_equity_uses_the_equity_endpoint(self):
        provider = RecordingProvider(supported_prefixes=(".SH",))
        reg = ProviderRegistry([provider], order=["recording"])

        reg.fetch_daily("600519.SH", *WINDOW, adjust="raw")
        assert provider.calls == ["600519.SH"]


class TestRealProviderBoundaries:
    """Boundary declarations must match the measured provider behaviour."""

    def test_baostock_declines_beijing(self):
        from stockmaster.providers.baostock_provider import BaostockProvider

        provider = BaostockProvider()
        assert provider.supports("600519.SH") is True
        assert provider.supports("000001.SZ") is True
        assert provider.supports("430047.BJ") is False
        assert provider.supports("920000.BJ") is False

    def test_tencent_declines_beijing(self):
        from stockmaster.providers.tencent_provider import TencentProvider

        provider = TencentProvider()
        assert provider.supports("600519.SH") is True
        assert provider.supports("300750.SZ") is True
        assert provider.supports("920000.BJ") is False
        assert provider.supports("430047.BJ") is False

    def test_sina_serves_only_the_920_range(self):
        from stockmaster.providers.sina_provider import SinaProvider

        provider = SinaProvider()
        assert provider.supports("600519.SH") is True
        assert provider.supports("920000.BJ") is True
        # Older BJ prefixes are not reachable from any configured source.
        assert provider.supports("430047.BJ") is False
        assert provider.supports("830799.BJ") is False
        assert provider.supports("871981.BJ") is False

    def test_mock_provider_supports_everything(self):
        from stockmaster.providers.mock_provider import MockProvider

        provider = MockProvider()
        for code in ("600519.SH", "000001.SZ", "300750.SZ", "430047.BJ", "920000.BJ"):
            assert provider.supports(code) is True
