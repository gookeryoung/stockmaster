"""Alert engine tests.

The most important guarantee is **idempotent triggering**: scanning repeatedly
must not duplicate alerts, or a simple threshold rule would flood the user. That
is asserted directly by running the scan multiple times.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.alert.channels import DatabaseChannel, Notification, WebhookChannel
from app.alert.engine import AlertEngine
from app.alert.rules import (
    RULE_TYPES,
    RuleContext,
    describe_rule,
    normalize_params,
)
from app.core.errors import DataError
from app.db import session_scope
from app.db.models import AlertRule, AlertTrigger, WatchlistItem
from app.providers.base import Bar
from app.sync import repository as repo


def business_days(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def ctx(**overrides) -> RuleContext:
    """A minimal context for rule-level tests."""
    base = dict(
        code="600519.SH",
        name="测试",
        trade_date=date(2026, 1, 5),
        close=100.0,
        preclose=99.0,
        open=99.5,
        high=101.0,
        low=99.0,
        volume=1_000_000.0,
        amount=1e8,
        pct_chg=1.01,
        turn=2.0,
        is_st=False,
    )
    base.update(overrides)
    return RuleContext(**base)


class TestNormalizeParams:
    def test_applies_defaults(self):
        params = normalize_params("volume_ratio", {})
        assert params == {"direction": "above", "threshold": 2.0, "window": 5}

    def test_coerces_string_numbers(self):
        params = normalize_params("price_cross", {"threshold": "120.5"})
        assert params["threshold"] == pytest.approx(120.5)

    def test_required_parameter_enforced(self):
        with pytest.raises(DataError, match="required"):
            normalize_params("price_cross", {})

    def test_invalid_choice_rejected(self):
        with pytest.raises(DataError, match="must be one of"):
            normalize_params("price_cross", {"threshold": 1, "direction": "sideways"})

    def test_unknown_parameter_rejected(self):
        """Typos must fail loudly rather than being silently ignored."""
        with pytest.raises(DataError, match="unknown parameters"):
            normalize_params("price_cross", {"threshold": 1, "threshhold": 2})

    def test_unknown_rule_type_rejected(self):
        with pytest.raises(DataError, match="unknown rule_type"):
            normalize_params("not_a_rule", {})

    def test_json_string_accepted(self):
        params = normalize_params("price_cross", '{"threshold": 50}')
        assert params["threshold"] == pytest.approx(50.0)

    def test_malformed_json_rejected(self):
        with pytest.raises(DataError, match="not valid JSON"):
            normalize_params("price_cross", "{not json}")

    def test_every_declared_rule_type_normalizes(self):
        """Guards against a rule type that can never be created."""
        for rule_type, spec in RULE_TYPES.items():
            params = {}
            for key, param in spec["params"].items():
                if param.get("required"):
                    params[key] = 1.0
            normalize_params(rule_type, params)

    def test_describe_rule_is_readable(self):
        text = describe_rule("price_cross", {"threshold": 100, "direction": "above"})
        assert "price_cross" in text and "100" in text


class TestRuleEvaluation:
    def test_price_cross_above(self):
        from app.alert.engine import evaluate_rule

        fired, message, snapshot = evaluate_rule(
            ctx(close=110.0), "price_cross", {"threshold": 105, "direction": "above"}
        )
        assert fired and "110.00" in message

    def test_price_cross_below(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(close=90.0), "price_cross", {"threshold": 95, "direction": "below"}
        )
        assert fired

    def test_price_cross_not_triggered(self):
        from app.alert.engine import evaluate_rule

        fired, message, _ = evaluate_rule(
            ctx(close=100.0), "price_cross", {"threshold": 105, "direction": "above"}
        )
        assert not fired and message == ""

    def test_pct_change_uses_derived_value(self):
        from app.alert.engine import evaluate_rule

        fired, _, snapshot = evaluate_rule(
            ctx(close=110.0, preclose=100.0, pct_chg=None),
            "pct_change",
            {"threshold": 9.0, "direction": "above"},
        )
        assert fired
        assert snapshot["pct_chg"] == pytest.approx(10.0)

    def test_volume_ratio(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(indicators={"vol_ratio": 3.5}),
            "volume_ratio",
            {"threshold": 2.0, "direction": "above"},
        )
        assert fired
        fired, _, _ = evaluate_rule(
            ctx(indicators={"vol_ratio": 1.0}),
            "volume_ratio",
            {"threshold": 2.0, "direction": "above"},
        )
        assert not fired

    def test_missing_value_does_not_trigger(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(indicators={}), "volume_ratio", {"threshold": 2.0}
        )
        assert not fired

    def test_ma_golden_cross(self):
        from app.alert.engine import evaluate_rule

        fired, message, _ = evaluate_rule(
            ctx(indicators={"ma5": 101.0, "ma20": 100.0, "ma5_prev": 99.0, "ma20_prev": 100.0}),
            "ma_cross",
            {"fast": 5, "slow": 20, "direction": "golden"},
        )
        assert fired and "金叉" in message

    def test_ma_no_cross_when_already_above(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(indicators={"ma5": 102.0, "ma20": 100.0, "ma5_prev": 101.0, "ma20_prev": 100.0}),
            "ma_cross",
            {"fast": 5, "slow": 20, "direction": "golden"},
        )
        assert not fired

    def test_ma_death_cross(self):
        from app.alert.engine import evaluate_rule

        fired, message, _ = evaluate_rule(
            ctx(indicators={"ma5": 99.0, "ma20": 100.0, "ma5_prev": 101.0, "ma20_prev": 100.0}),
            "ma_cross",
            {"fast": 5, "slow": 20, "direction": "death"},
        )
        assert fired and "死叉" in message

    def test_rsi_oversold(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(indicators={"rsi6": 20.0}),
            "rsi_extreme",
            {"period": 6, "direction": "oversold", "threshold": 30.0},
        )
        assert fired

    def test_kdj_golden_cross(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(indicators={"kdj_k": 25.0, "kdj_d": 20.0, "kdj_k_prev": 18.0, "kdj_d_prev": 20.0}),
            "kdj_cross",
            {"direction": "golden"},
        )
        assert fired

    def test_boll_upper_break(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(close=110.0, indicators={"boll_up": 105.0}),
            "boll_break",
            {"band": "upper"},
        )
        assert fired

    def test_new_high_uses_prior_window(self):
        from app.alert.engine import evaluate_rule

        history = [{"high": 10.0, "low": 9.0, "close": 9.5} for _ in range(20)]
        history[-1] = {"high": 15.0, "low": 9.0, "close": 14.0}
        fired, message, _ = evaluate_rule(
            ctx(history=history), "new_high", {"window": 10, "direction": "high"}
        )
        assert fired and "新高" in message

    def test_new_high_insufficient_history(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(history=[{"high": 10.0, "low": 9.0}]), "new_high", {"window": 20}
        )
        assert not fired

    def test_prediction_threshold(self):
        from app.alert.engine import evaluate_rule

        fired, message, _ = evaluate_rule(
            ctx(prediction={"prob_up": 0.75}),
            "prediction",
            {"threshold": 0.6, "direction": "above", "horizon": 1},
        )
        assert fired and "75.00%" in message

    def test_drawdown_from_peak(self):
        from app.alert.engine import evaluate_rule

        history = [{"close": 100.0}] * 30 + [{"close": 80.0}]
        fired, message, snapshot = evaluate_rule(
            ctx(history=history), "drawdown", {"window": 60, "threshold": 10.0}
        )
        assert fired
        assert snapshot["drawdown_pct"] == pytest.approx(20.0)

    def test_factor_score(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(factors={"score": 1.8}),
            "factor_score",
            {"direction": "above", "threshold": 1.0},
        )
        assert fired

    def test_turnover(self):
        from app.alert.engine import evaluate_rule

        fired, _, _ = evaluate_rule(
            ctx(turn=8.0), "turnover", {"direction": "above", "threshold": 5.0}
        )
        assert fired


@pytest.fixture
def engine_db(tmp_path, monkeypatch):
    """Fresh database with a small watchlist and synthetic price history."""
    import uuid

    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"alert-{uuid.uuid4().hex}.db")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    days = business_days(60)
    bars = [
        Bar(
            code="600519.SH",
            trade_date=day,
            open=10.0,
            high=10.5,
            low=9.5,
            close=10.0 + index * 0.1,
            volume=1_000_000.0,
            amount=1e8,
            preclose=10.0 + (index - 1) * 0.1 if index else 10.0,
            turn=1.5,
            is_st=False,
            adjust="qfq",
        )
        for index, day in enumerate(days)
    ]
    with session_scope() as session:
        repo.upsert_daily_bars(session, "600519.SH", bars, "qfq")
        repo.upsert_instruments(
            session,
            [
                __import__("app.providers.base", fromlist=["InstrumentInfo"]).InstrumentInfo(
                    code="600519.SH", name="测试股票", market="SH", status=1
                )
            ],
        )
        session.add(WatchlistItem(list_name="default", code="600519.SH"))

    yield
    session_mod.reset_engine()
    get_settings.cache_clear()


class TestAlertEngineDedup:
    def test_creates_rule_and_triggers(self, engine_db):
        engine = AlertEngine()
        rule_id = engine.create_rule(
            name="价格突破",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        triggers = engine.evaluate_all()
        assert triggers
        assert all(t.rule_id == rule_id for t in triggers)

    def test_repeated_scans_do_not_duplicate(self, engine_db):
        """The core idempotency guarantee."""
        engine = AlertEngine()
        engine.create_rule(
            name="价格突破",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )

        first = engine.evaluate_all()
        assert len(first) == 1

        # Scan four more times; nothing new should be produced.
        for _ in range(4):
            assert engine.evaluate_all() == []

        with session_scope() as session:
            rows = session.execute(select(AlertTrigger)).scalars().all()
        assert len(rows) == 1

    def test_once_per_day_collapses_to_single_trigger(self, engine_db):
        engine = AlertEngine()
        engine.create_rule(
            name="日线突破",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
            once_per_day=True,
        )
        engine.evaluate_all()
        # A different wall-clock time on the same trade date must not re-alert.
        later = datetime(2026, 3, 1, 15, 30)
        assert engine.evaluate_all(now=later) == []

    def test_cooldown_creates_new_bucket(self, engine_db):
        """With a cooldown, a later time bucket is a genuinely new alert."""
        engine = AlertEngine()
        engine.create_rule(
            name="冷却规则",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
            once_per_day=False,
            cooldown_seconds=60,
        )
        base = datetime(2026, 3, 1, 10, 0, 0)
        first = engine.evaluate_all(now=base)
        assert len(first) == 1
        # Same bucket: suppressed.
        assert engine.evaluate_all(now=base + timedelta(seconds=10)) == []
        # Next bucket: allowed.
        second = engine.evaluate_all(now=base + timedelta(seconds=70))
        assert len(second) == 1

    def test_trigger_snapshot_is_recorded(self, engine_db):
        import json

        engine = AlertEngine()
        engine.create_rule(
            name="价格突破",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        engine.evaluate_all()
        with session_scope() as session:
            row = session.execute(select(AlertTrigger)).scalar_one()
            snapshot = json.loads(row.snapshot_json)
        assert "close" in snapshot and "threshold" in snapshot
        assert row.message
        assert row.status == "new"

    def test_disabled_rule_is_skipped(self, engine_db):
        engine = AlertEngine()
        rule_id = engine.create_rule(
            name="停用规则",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
            enabled=False,
        )
        assert engine.evaluate_all() == []
        with session_scope() as session:
            rule = session.get(AlertRule, rule_id)
            assert rule is not None and rule.enabled is False

    def test_rule_not_matching_produces_nothing(self, engine_db):
        engine = AlertEngine()
        engine.create_rule(
            name="不可能触发",
            rule_type="price_cross",
            params={"threshold": 1_000_000.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        assert engine.evaluate_all() == []

    def test_scope_codes_limits_evaluation(self, engine_db):
        engine = AlertEngine()
        engine.create_rule(
            name="指定代码",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="codes",
            scope_value="600519.SH",
        )
        assert len(engine.evaluate_all()) == 1

    def test_empty_scope_produces_nothing(self, engine_db):
        engine = AlertEngine()
        engine.create_rule(
            name="空范围",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="codes",
            scope_value="",
        )
        assert engine.evaluate_all() == []

    def test_evaluate_specific_rule_ids(self, engine_db):
        engine = AlertEngine()
        first = engine.create_rule(
            name="规则一",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        engine.create_rule(
            name="规则二",
            rule_type="price_cross",
            params={"threshold": 11.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        triggers = engine.evaluate_all(rule_ids=[first])
        assert triggers and all(t.rule_id == first for t in triggers)

    def test_min_amount_filter(self, engine_db):
        engine = AlertEngine()
        engine.create_rule(
            name="高成交额",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
            min_amount=1e12,
        )
        assert engine.evaluate_all() == []

    def test_create_rule_rejects_unknown_type(self, engine_db):
        with pytest.raises(DataError):
            AlertEngine().create_rule(name="x", rule_type="bogus")

    def test_purge_old_triggers(self, engine_db):
        engine = AlertEngine()
        engine.create_rule(
            name="价格突破",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        engine.evaluate_all(now=datetime(2020, 1, 1, 10, 0))
        removed = engine.purge_old_triggers(days=30)
        assert removed == 1

    def test_market_hours_gate_is_opt_in(self, engine_db):
        engine = AlertEngine(respect_market_hours=True)
        engine.create_rule(
            name="价格突破",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        # 08:00 UTC is 16:00 Beijing — after the close, so the gate blocks the scan.
        assert engine.evaluate_all(now=datetime(2026, 3, 2, 8, 0)) == []
        # 02:00 UTC is 10:00 Beijing — inside the morning session, so it proceeds.
        assert engine.evaluate_all(now=datetime(2026, 3, 2, 2, 0)) != []

    def test_market_hours_gate_disabled_by_default(self, engine_db):
        engine = AlertEngine()
        engine.create_rule(
            name="价格突破",
            rule_type="price_cross",
            params={"threshold": 10.0, "direction": "above"},
            scope_type="watchlist",
            scope_value="default",
        )
        # Without the gate, an after-hours scan still evaluates (used by jobs).
        assert engine.evaluate_all(now=datetime(2026, 3, 2, 8, 0)) != []

    def test_malformed_stored_rule_is_skipped(self, engine_db):
        """A corrupt rule must not abort the whole scan."""
        with session_scope() as session:
            session.add(
                AlertRule(
                    name="坏规则",
                    rule_type="price_cross",
                    params_json="{}",  # missing required threshold
                    scope_type="watchlist",
                    scope_value="default",
                    enabled=True,
                )
            )
        engine = AlertEngine()
        assert engine.evaluate_all() == []


class TestChannels:
    def test_database_channel_always_succeeds(self):
        assert DatabaseChannel().send(Notification(title="t", body="b")) is True

    def test_webhook_without_url_fails_softly(self):
        assert WebhookChannel("").send(Notification(title="t", body="b")) is False

    def test_notify_returns_per_channel_counts(self):
        from app.alert.channels import notify

        results = notify([Notification(title="t", body="b")])
        assert "database" in results
        assert results["database"] == 1
