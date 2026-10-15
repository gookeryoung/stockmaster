"""Symbol normalisation and market-mechanics tests."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.errors import DataError
from app.core.market import (
    LOT_SIZE,
    add_trade_days,
    is_call_auction,
    is_limit_down,
    is_limit_up,
    is_one_word_limit,
    is_trading_time,
    limit_prices,
    lot_floor,
    next_trade_day_after,
    prev_trade_day,
    price_limit_ratio,
)
from app.core.symbols import (
    from_baostock,
    infer_exchange,
    normalize,
    split,
    to_baostock,
    to_eastmoney_secid,
    to_sina,
    to_tencent,
)


class TestSymbols:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("600519", "600519.SH"),
            ("sh600519", "600519.SH"),
            ("sh.600519", "600519.SH"),
            ("600519.SH", "600519.SH"),
            ("600519.sh", "600519.SH"),
            ("1.600519", "600519.SH"),
            ("000001", "000001.SZ"),
            ("sz000001", "000001.SZ"),
            ("0.000001", "000001.SZ"),
            ("300750", "300750.SZ"),
            ("688981", "688981.SH"),
            (" 600519.SH  ", "600519.SH"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize(raw) == expected

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "12345", "999999"])
    def test_normalize_rejects_garbage(self, bad):
        with pytest.raises(DataError):
            normalize(bad)

    def test_infer_exchange_boards(self):
        assert infer_exchange("600519") == "SH"
        assert infer_exchange("688981") == "SH"
        assert infer_exchange("000001") == "SZ"
        assert infer_exchange("300750") == "SZ"
        assert infer_exchange("830799") == "BJ"

    def test_split(self):
        assert split("600519.SH") == ("600519", "SH")

    def test_provider_forms(self):
        assert to_baostock("600519.SH") == "sh.600519"
        assert to_baostock("000001.SZ") == "sz.000001"
        assert to_tencent("600519.SH") == "sh600519"
        assert to_sina("000001.SZ") == "sz000001"
        assert to_eastmoney_secid("600519.SH") == "1.600519"
        assert to_eastmoney_secid("000001.SZ") == "0.000001"

    def test_from_baostock_roundtrip(self):
        assert from_baostock("sh.600519") == "600519.SH"
        assert from_baostock("sz.000001") == "000001.SZ"


class TestPriceLimits:
    def test_main_board_ten_percent(self):
        assert limit_prices(100.0, 0.10) == (90.0, 110.0)

    def test_rounding_is_half_up(self):
        # 10.03 * 0.05 = 0.5015 -> half-up at 2dp
        assert limit_prices(10.03, 0.05) == (9.53, 10.53)

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("600519.SH", 0.10),
            ("000001.SZ", 0.10),
            ("300750.SZ", 0.20),  # 创业板
            ("301001.SZ", 0.20),
            ("688981.SH", 0.20),  # 科创板
            ("689009.SH", 0.20),
        ],
    )
    def test_board_ratios(self, code, expected):
        assert price_limit_ratio(code) == pytest.approx(expected)

    def test_st_main_board_is_five_percent(self):
        assert price_limit_ratio("600519.SH", is_st=True) == pytest.approx(0.05)

    def test_st_does_not_change_star_board(self):
        # STAR/ChiNext keep 20% regardless of ST status.
        assert price_limit_ratio("688981.SH", is_st=True) == pytest.approx(0.20)

    def test_bse_is_thirty_percent(self):
        assert price_limit_ratio("830799.BJ") == pytest.approx(0.30)

    def test_zero_preclose_is_safe(self):
        assert limit_prices(0.0, 0.10) == (0.0, 0.0)

    def test_is_limit_up_and_down(self):
        assert is_limit_up(110.0, 100.0, "600519.SH")
        assert not is_limit_up(109.99, 100.0, "600519.SH")
        assert is_limit_down(90.0, 100.0, "600519.SH")
        assert not is_limit_down(90.01, 100.0, "600519.SH")

    def test_chinext_twenty_percent(self):
        # 20% band: 100 -> 120
        assert is_limit_up(120.0, 100.0, "300750.SZ")
        assert not is_limit_up(115.0, 100.0, "300750.SZ")

    def test_one_word_limit(self):
        assert is_one_word_limit(110.0, 110.0, 110.0, 100.0, "600519.SH")
        assert not is_one_word_limit(110.0, 111.0, 110.0, 100.0, "600519.SH")
        assert not is_one_word_limit(100.0, 101.0, 99.0, 100.0, "600519.SH")


class TestSessions:
    def test_trading_time(self):
        tz = __import__("app.config", fromlist=["settings"]).settings.tz
        assert is_trading_time(datetime(2026, 10, 15, 10, 0, tzinfo=tz))
        assert is_trading_time(datetime(2026, 10, 15, 14, 0, tzinfo=tz))
        assert not is_trading_time(datetime(2026, 10, 15, 12, 0, tzinfo=tz))
        assert not is_trading_time(datetime(2026, 10, 15, 16, 0, tzinfo=tz))

    def test_call_auction(self):
        tz = __import__("app.config", fromlist=["settings"]).settings.tz
        assert is_call_auction(datetime(2026, 10, 15, 9, 20, tzinfo=tz))
        assert not is_call_auction(datetime(2026, 10, 15, 10, 0, tzinfo=tz))


class TestLotsAndCalendar:
    def test_lot_floor(self):
        assert lot_floor(0) == 0
        assert lot_floor(99) == 0
        assert lot_floor(100) == 100
        assert lot_floor(358) == 300
        assert lot_floor(199.9) == 100
        assert LOT_SIZE == 100

    def test_calendar_helpers(self, trade_days):
        ref = trade_days[5]
        assert next_trade_day_after(trade_days, ref) == trade_days[6]
        assert prev_trade_day(trade_days, ref) == trade_days[4]
        assert add_trade_days(trade_days, ref, 3) == trade_days[8]
        assert add_trade_days(trade_days, ref, -2) == trade_days[3]
        assert add_trade_days(trade_days, ref, 0) == ref
        # Beyond the end returns None rather than raising.
        assert add_trade_days(trade_days, trade_days[-1], 1) is None
