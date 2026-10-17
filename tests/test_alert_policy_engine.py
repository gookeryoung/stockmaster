"""工作区级预警策略在扫描引擎中的生效测试。

策略语义是「只收紧、不放宽」：``exclude_st`` / ``once_per_day`` 任一为 True 即生效，
``min_amount`` / ``cooldown_seconds`` 取规则与策略的较大值。这里不跑真实行情，而是
把 scope 解析与 context 构建打桩，只验证引擎在过滤与去重两个环节确实用了有效值。

对照 ``tests/test_workspace_alert_policy.py``（API 与数据层），本文件专门覆盖
``AlertEngine.evaluate_all`` 的集成行为。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from stockmaster.db import session_scope
from stockmaster.db.models import AlertRule, Workspace, WorkspaceAlertPolicy
from stockmaster.plugins.alerts.services.engine import AlertEngine, _dedup_key
from stockmaster.plugins.alerts.services.policy import DEFAULT_POLICY, EffectivePolicy, effective_policy
from stockmaster.plugins.alerts.services.rules import RuleContext

TRADE_DATE = date(2026, 1, 5)


def _ctx(**overrides) -> RuleContext:
    base = {
        "code": "600519.SH",
        "name": "测试",
        "trade_date": TRADE_DATE,
        "close": 100.0,
        "preclose": 99.0,
        "open": 99.5,
        "high": 101.0,
        "low": 99.0,
        "volume": 1_000_000.0,
        "amount": 1e8,
        "pct_chg": 1.01,
        "turn": 2.0,
        "is_st": False,
    }
    base.update(overrides)
    return RuleContext(**base)


def _workspace(name: str) -> int:
    with session_scope() as session:
        workspace = Workspace(name=name)
        session.add(workspace)
        session.flush()
        return int(workspace.id)


def _rule(workspace_id: int, **overrides) -> int:
    fields = {
        "workspace_id": workspace_id,
        "name": "r",
        "rule_type": "price_cross",
        "params_json": json.dumps({"threshold": 10.0, "direction": "above"}),
        "scope_type": "codes",
        "scope_value": "600519.SH",
        "enabled": True,
        "once_per_day": False,
        "cooldown_seconds": 0,
        "min_amount": 0.0,
        "exclude_st": False,
    }
    fields.update(overrides)
    with session_scope() as session:
        rule = AlertRule(**fields)
        session.add(rule)
        session.flush()
        return int(rule.id)


def _policy(workspace_id: int, **overrides) -> None:
    with session_scope() as session:
        session.add(WorkspaceAlertPolicy(workspace_id=workspace_id, **overrides))


@pytest.fixture
def scan(monkeypatch, temp_db):
    """把引擎的 scope/context 换成固定值；返回 (run, ctx_setter)。"""

    state = {"ctx": _ctx()}

    monkeypatch.setattr(AlertEngine, "_resolve_scope", staticmethod(lambda session, rule: ["600519.SH"]))
    monkeypatch.setattr(AlertEngine, "_build_context", staticmethod(lambda session, code: state["ctx"]))

    def set_ctx(ctx: RuleContext) -> None:
        state["ctx"] = ctx

    def run(rule_ids: list[int]) -> list:
        return AlertEngine().evaluate_all(rule_ids=rule_ids, persist=False)

    return run, set_ctx


class TestExcludeSt:
    def test_policy_exclude_st_filters_candidate(self, scan):
        run, set_ctx = scan
        workspace_id = _workspace("ws-st-policy")
        rule_id = _rule(workspace_id)
        _policy(workspace_id, exclude_st=True)
        set_ctx(_ctx(is_st=True))
        assert run([rule_id]) == []

    def test_without_policy_st_rule_fires(self, scan):
        run, set_ctx = scan
        workspace_id = _workspace("ws-st-no-policy")
        rule_id = _rule(workspace_id)
        set_ctx(_ctx(is_st=True))
        candidates = run([rule_id])
        assert len(candidates) == 1 and candidates[0].rule_id == rule_id

    def test_rule_setting_still_stricter(self, scan):
        """策略只在规则之上收紧：规则自己排除 ST、策略未设置时同样生效。"""
        run, set_ctx = scan
        workspace_id = _workspace("ws-st-rule")
        rule_id = _rule(workspace_id, exclude_st=True)
        set_ctx(_ctx(is_st=True))
        assert run([rule_id]) == []


class TestMinAmountFloor:
    def test_policy_floor_blocks_small_amount(self, scan):
        run, set_ctx = scan
        workspace_id = _workspace("ws-amount-floor")
        rule_id = _rule(workspace_id, min_amount=0.0)
        _policy(workspace_id, min_amount=1_000_000.0)
        set_ctx(_ctx(amount=1_000.0))
        assert run([rule_id]) == []

    def test_policy_floor_keeps_large_amount(self, scan):
        run, set_ctx = scan
        workspace_id = _workspace("ws-amount-ok")
        rule_id = _rule(workspace_id)
        _policy(workspace_id, min_amount=1_000_000.0)
        set_ctx(_ctx(amount=1e8))
        assert len(run([rule_id])) == 1

    def test_rule_threshold_wins_when_stricter(self, scan):
        """策略不能放宽规则：规则门槛更高时按规则的来。"""
        run, set_ctx = scan
        workspace_id = _workspace("ws-amount-rule")
        rule_id = _rule(workspace_id, min_amount=5_000_000.0)
        _policy(workspace_id, min_amount=1_000.0)
        set_ctx(_ctx(amount=400_000.0))
        assert run([rule_id]) == []


class TestDedupWindow:
    def test_effective_values_are_used_by_dedup_key(self):
        rule = AlertRule(
            id=7, name="r", rule_type="price_cross", params_json="{}", once_per_day=False, cooldown_seconds=0
        )
        ctx = _ctx()
        now = datetime(2026, 1, 5, 10, 0)

        # 策略把冷却从 0 收紧到 3600 秒：key 进入 cooldown bucket 形态。
        tightened = _dedup_key(rule, ctx, now, cooldown_seconds=3600, once_per_day=False)
        assert ":b" in tightened
        # 策略打开「每天最多一次」：不再带 bucket / minute 后缀。
        once = _dedup_key(rule, ctx, now, cooldown_seconds=3600, once_per_day=True)
        assert once == f"{rule.id}:{ctx.code}:{TRADE_DATE.isoformat()}"

    def test_policy_cooldown_changes_scan_dedup_key(self, scan):
        run, set_ctx = scan
        workspace_id = _workspace("ws-cooldown")
        rule_id = _rule(workspace_id, once_per_day=False, cooldown_seconds=0)
        _policy(workspace_id, cooldown_seconds=3600)
        set_ctx(_ctx(close=100.0))
        candidates = run([rule_id])
        assert len(candidates) == 1 and ":b" in candidates[0].dedup_key

    def test_policy_once_per_day_collapses_key(self, scan):
        run, set_ctx = scan
        workspace_id = _workspace("ws-once")
        rule_id = _rule(workspace_id, once_per_day=False, cooldown_seconds=0)
        _policy(workspace_id, once_per_day=True)
        set_ctx(_ctx(close=100.0))
        candidates = run([rule_id])
        assert len(candidates) == 1 and candidates[0].dedup_key == f"{rule_id}:600519.SH:{TRADE_DATE.isoformat()}"


class TestIsolationBetweenWorkspaces:
    def test_policy_only_affects_its_workspace(self, scan):
        run, set_ctx = scan
        strict_ws = _workspace("ws-strict")
        free_ws = _workspace("ws-free")
        strict_rule = _rule(strict_ws)
        free_rule = _rule(free_ws)
        _policy(strict_ws, exclude_st=True)
        set_ctx(_ctx(is_st=True))
        candidates = run([strict_rule, free_rule])
        assert [c.rule_id for c in candidates] == [free_rule]


class TestEffectivePolicyNormalisation:
    def test_missing_row_returns_default(self):
        assert effective_policy(None) is DEFAULT_POLICY

    def test_negative_stored_values_are_clamped(self):
        """绕过 API 直接改库的脏数据不能让有效值变成负数。"""
        row = WorkspaceAlertPolicy(
            workspace_id=1,
            exclude_st=None,
            min_amount=-5.0,
            cooldown_seconds=-60,
            once_per_day=None,
        )
        effective = effective_policy(row)
        assert effective == EffectivePolicy(exclude_st=False, min_amount=0.0, cooldown_seconds=0, once_per_day=False)

    def test_values_pass_through(self):
        row = WorkspaceAlertPolicy(
            workspace_id=1,
            exclude_st=True,
            min_amount=123.5,
            cooldown_seconds=600,
            once_per_day=True,
        )
        assert effective_policy(row) == EffectivePolicy(
            exclude_st=True, min_amount=123.5, cooldown_seconds=600, once_per_day=True
        )
