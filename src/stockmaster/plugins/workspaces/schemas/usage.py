"""工作区用量趋势的响应模型。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class WorkspaceUsagePointOut(BaseModel):
    """趋势图上的一个点：某个工作区在某一天的用量快照。

    分项与 ``quota.USAGE_COUNTERS`` 的键一一对应，``data_total`` 是派生的聚合值，
    前端不需要自己再相加。
    """

    model_config = ConfigDict(from_attributes=True)

    captured_on: date
    data_total: int
    member_count: int
    watchlist_items: int
    alert_rules: int
    backtest_runs: int


__all__ = ["WorkspaceUsagePointOut"]
