"""Live provider contract tests.

These hit real endpoints and are therefore **deselected by default**:

    pytest -m network

They exist because the project's hard-won provider knowledge (Eastmoney being
blocked from Python, baostock's shared socket, Tencent/Sina agreeing on qfq
prices) must be re-verifiable after any dependency bump. A silent contract change
upstream is one of the highest-impact failure modes for this app.
"""

from __future__ import annotations

from datetime import date

import pytest

pytestmark = pytest.mark.network

# A window that is in the past relative to the recorded project baseline
# (2026-10-15) yet recent enough to be present in every source.
WINDOW_START = date(2026, 9, 1)
WINDOW_END = date(2026, 10, 15)

# Cross-source baseline captured from all three providers during discovery.
BASELINE_LAST_CLOSE = 1312.97  # 600519.SH, 2026-10-15, qfq
BASELINE_CSI300_CLOSE = 4166.01  # 000300.SH, 2026-10-15


@pytest.fixture(scope="module")
def baostock():
    from stockmaster.providers.baostock_provider import BaostockProvider

    provider = BaostockProvider(timeout=30.0)
    provider.login()
    yield provider
    provider.close()


class TestBaostockContract:
    def test_calendar_returns_open_days(self, baostock):
        days = baostock.fetch_calendar(date(2026, 10, 1), date(2026, 10, 15))
        assert days, "calendar should not be empty"
        assert all(isinstance(d, date) for d in days)
        # National Day holiday 2026-10-01..07 must be excluded.
        assert date(2026, 10, 1) not in days
        assert date(2026, 10, 15) in days

    def test_daily_fields_are_complete(self, baostock):
        bars = baostock.fetch_daily("600519.SH", date(2026, 10, 8), date(2026, 10, 15), "raw")
        assert bars
        last = bars[-1]
        # These five fields cannot be derived elsewhere and gate limit/ST logic.
        assert last.preclose is not None
        assert last.pct_chg is not None
        assert last.turn is not None
        assert last.pe_ttm is not None
        assert last.is_st is False
        for bar in bars:
            assert bar.low <= bar.open <= bar.high
            assert bar.low <= bar.close <= bar.high

    def test_qfq_matches_recorded_baseline(self, baostock):
        bars = baostock.fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "qfq")
        assert bars
        assert bars[-1].trade_date == date(2026, 10, 15)
        assert bars[-1].close == pytest.approx(BASELINE_LAST_CLOSE, abs=0.01)

    def test_adjust_modes_differ_where_expected(self, baostock):
        raw = baostock.fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "raw")
        hfq = baostock.fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "hfq")
        # 后复权 is anchored at listing, so it must be far above the raw price
        # for a long-listed high-priced name like 600519.
        assert hfq[-1].close > raw[-1].close * 5

    def test_index_daily(self, baostock):
        bars = baostock.fetch_index_daily("000300.SH", WINDOW_START, WINDOW_END)
        assert bars
        assert bars[-1].close == pytest.approx(BASELINE_CSI300_CLOSE, abs=1.0)

    def test_stock_basic_has_ipo_date(self, baostock):
        info = baostock.fetch_stock_basic("600519.SH")
        assert info is not None
        assert info.list_date == date(2001, 8, 27)

    def test_socket_timeout_is_installed(self, baostock):
        """Regression guard for the unbounded-recv hang.

        baostock creates its socket without a timeout; the provider must set one
        so a stalled server raises instead of hanging the sync forever.
        """
        from baostock.common import context

        sock = getattr(context, "default_socket", None)
        assert sock is not None, "expected an active baostock socket"
        assert sock.gettimeout() is not None, "socket timeout was not applied"


class TestTencentContract:
    def test_daily_parses_and_matches_baseline(self):
        from stockmaster.providers.tencent_provider import TencentProvider

        p = TencentProvider()
        bars = p.fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "qfq")
        assert bars
        assert bars[-1].close == pytest.approx(BASELINE_LAST_CLOSE, abs=0.01)

    def test_calendar(self):
        from stockmaster.providers.tencent_provider import TencentProvider

        days = TencentProvider().fetch_calendar(date(2026, 10, 1), date(2026, 10, 15))
        assert days and date(2026, 10, 15) in days


class TestSinaContract:
    def test_daily_parses_and_matches_baseline(self):
        from stockmaster.providers.sina_provider import SinaProvider

        bars = SinaProvider().fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "qfq")
        assert bars
        assert bars[-1].close == pytest.approx(BASELINE_LAST_CLOSE, abs=0.01)

    def test_spot_snapshot_parses_positionally(self):
        """The snapshot has Chinese headers; parsing must not depend on locale."""
        from stockmaster.providers.sina_provider import SinaProvider

        snaps = SinaProvider().fetch_snapshot(["600519.SH", "000001.SZ"])
        assert snaps, "snapshot should return rows"
        codes = {s.code for s in snaps}
        assert codes <= {"600519.SH", "000001.SZ"}
        sample = next(s for s in snaps if s.code == "600519.SH")
        assert sample.price is not None and sample.price > 0
        assert sample.code == "600519.SH"


class TestCrossSourceConsistency:
    def test_qfq_close_agrees_across_sources(self):
        """All three sources must agree on the qfq close.

        This is the invariant that makes failover safe: if the backup sources
        disagreed materially, silently switching sources would corrupt the series.
        """
        from stockmaster.providers.baostock_provider import BaostockProvider
        from stockmaster.providers.sina_provider import SinaProvider
        from stockmaster.providers.tencent_provider import TencentProvider

        bs = BaostockProvider(timeout=30.0)
        bs.login()
        try:
            b = bs.fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "qfq")
        finally:
            bs.close()

        t = TencentProvider().fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "qfq")
        s = SinaProvider().fetch_daily("600519.SH", WINDOW_START, WINDOW_END, "qfq")

        closes = {
            "baostock": {x.trade_date: x.close for x in b},
            "tencent": {x.trade_date: x.close for x in t},
            "sina": {x.trade_date: x.close for x in s},
        }
        common = set.intersection(*(set(v) for v in closes.values()))
        assert len(common) >= 5, f"too few overlapping dates: {sorted(common)}"
        for day in sorted(common):
            values = [closes[src][day] for src in closes]
            assert max(values) - min(values) < 0.02, f"qfd close disagreement on {day}: " + ", ".join(
                f"{k}={v}" for k, v in closes.items()
            )
