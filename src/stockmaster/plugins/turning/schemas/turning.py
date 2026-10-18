"""turning 插件 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TurningPercentiles(BaseModel):
    """七维分位明细（0-100，越大越接近底部）。"""

    pos250: float | None = None
    bias60: float | None = None
    dd250: float | None = None
    rsi14: float | None = None
    rsi6: float | None = None
    bb_pos: float | None = None
    atr_pct: float | None = None


class TurningStatusRow(BaseModel):
    """单只标的的最新顶底状态。"""

    code: str
    name: str = ""
    trade_date: str | None = None
    close: float | None = None
    bottom: float | None = Field(default=None, description="底部综合分（最短板）")
    top_count: int | None = Field(default=None, description="顶部共振计数")
    gate: int = Field(description="闸门状态 1 正常 / 0 降档 / -1 挂起")
    hold_thr: float | None = Field(default=None, description="生效阈值（降档上浮 10 分）")
    candidate: bool = Field(default=False, description="当日是否为底部候选（冷却去重后）")
    entry: bool = Field(default=False, description="当日是否触发右侧确认买入信号")
    percentiles: TurningPercentiles | None = None


class TurningScanResponse(BaseModel):
    trade_date: str | None = None
    items: list[TurningStatusRow] = []
    missing: list[str] = []


class TurningEvent(BaseModel):
    trade_date: str
    kind: str = Field(description="bottom / top")
    close: float
    score: float = Field(description="底部最短板分或顶部共振计数")
    gate: int
    entry: bool = Field(default=False, description="底部事件是否获得右侧确认")


class TurningHistoryResponse(BaseModel):
    code: str
    name: str = ""
    kind: str
    events: list[TurningEvent] = []


class TurningRulesResponse(BaseModel):
    """模型口径说明（与 core/tpcore.describe 一致）。"""

    buy: list[str]
    sell: list[str]
    note: str
