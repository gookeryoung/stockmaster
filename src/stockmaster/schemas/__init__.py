"""Pydantic request/response schemas for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Adjust = Literal["raw", "qfq", "hfq"]


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class BarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trade_date: date
    open: float
    high: float
    low: float
    close: float
    preclose: float | None = None
    volume: float = 0.0
    amount: float = 0.0
    turn: float | None = None
    pct_chg: float | None = None
    is_st: bool = False


class BarsResponse(BaseModel):
    code: str
    name: str = ""
    adjust: Adjust = "qfq"
    count: int
    bars: list[BarOut]


class InstrumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str = ""
    market: str = ""
    board: str = ""
    is_index: bool = False
    is_st: bool = False
    status: int = 1
    list_date: date | None = None


class InstrumentPage(BaseModel):
    total: int
    items: list[InstrumentOut]


class CalendarResponse(BaseModel):
    start: date | None = None
    end: date | None = None
    days: list[date]


class IndexQuote(BaseModel):
    code: str
    name: str = ""
    close: float | None = None
    preclose: float | None = None
    pct_chg: float | None = None
    trade_date: date | None = None


class MarketOverview(BaseModel):
    as_of: date | None = None
    indices: list[IndexQuote]
    advancing: int = 0
    declining: int = 0
    unchanged: int = 0
    limit_up: int = 0
    limit_down: int = 0
    total: int = 0
    data_freshness: dict[str, Any] = Field(default_factory=dict)


class IndicatorSnapshot(BaseModel):
    code: str
    trade_date: date | None = None
    values: dict[str, float | None]


class MarketQuoteOut(BaseModel):
    """One row of the batched quote endpoint.

    ``source`` is per item because a single response can mix a live snapshot with
    daily fallbacks; ``trade_date`` is only set for daily rows and ``quote_time``
    only for snapshot rows.
    """

    code: str
    name: str = ""
    price: float | None = None
    preclose: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    amount: float | None = None
    pct_chg: float | None = None
    turnover: float | None = None
    trade_date: date | None = None
    quote_time: str = ""
    source: str = ""
    stale: bool = False


class MarketQuotesResponse(BaseModel):
    as_of: date | None = None
    source: Literal["snapshot", "daily"]
    items: list[MarketQuoteOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


class FactorSnapshot(BaseModel):
    code: str
    name: str = ""
    trade_date: date | None = None
    factors: dict[str, float | None]
    score: float | None = None
    percentiles: dict[str, float | None] = Field(default_factory=dict)


class FactorScoreRow(BaseModel):
    code: str
    name: str = ""
    score: float | None = None
    trade_date: date | None = None


class FactorBatchResponse(BaseModel):
    trade_date: date | None = None
    items: list[FactorScoreRow] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class ScreenRequest(BaseModel):
    trade_date: date | None = None
    min_score: float | None = None
    max_vol: float | None = None
    min_amount_log: float | None = None
    exclude_st: bool = True
    boards: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=500)


class ScreenRow(BaseModel):
    code: str
    name: str = ""
    trade_date: date | None = None
    score: float | None = None
    mom_20: float | None = None
    mom_60: float | None = None
    vol_20: float | None = None
    turnover_20: float | None = None
    rsi_14: float | None = None
    close: float | None = None


class CorrelationResponse(BaseModel):
    codes: list[str]
    matrix: list[list[float | None]]


# --------------------------------------------------------------------------- #
# Prediction
# --------------------------------------------------------------------------- #


class ModelCard(BaseModel):
    model_id: str
    algo: str
    horizon: int
    is_active: bool = False
    trained_at: datetime | None = None
    n_train: int = 0
    n_test: int = 0
    train_start: date | None = None
    train_end: date | None = None
    test_start: date | None = None
    test_end: date | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    importance: dict[str, float] = Field(default_factory=dict)


class PredictionOut(BaseModel):
    code: str
    name: str = ""
    trade_date: date | None = None
    horizon: int = 1
    prob_up: float | None = None
    model_id: str = ""


class PredictionListResponse(BaseModel):
    trade_date: date | None = None
    horizon: int = 1
    items: list[PredictionOut]


class TrainRequest(BaseModel):
    algo: Literal["hist_gbdt", "random_forest", "logistic"] = "hist_gbdt"
    horizon: int | None = Field(default=None, ge=1, le=60)
    codes: list[str] | None = None


class TrainResponse(BaseModel):
    model_id: str | None = None
    horizon: int | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] | None = None
    n_train: int = 0
    n_test: int = 0


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #


class BacktestRequest(BaseModel):
    strategy: Literal["buy_hold", "score_top_n", "prediction_top_n", "ma_cross"] = "buy_hold"
    codes: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None
    initial_cash: float = Field(default=1_000_000.0, gt=0)
    top_n: int = Field(default=10, ge=1, le=100)
    rebalance_days: int = Field(default=5, ge=1, le=60)
    benchmark: str | None = "000300.SH"
    commission_rate: float = Field(default=0.00025, ge=0, le=0.01)
    min_commission: float = Field(default=5.0, ge=0)
    stamp_tax_rate: float = Field(default=0.0005, ge=0, le=0.01)
    transfer_fee_rate: float = Field(default=0.00001, ge=0, le=0.01)
    slippage_rate: float = Field(default=0.0005, ge=0, le=0.05)
    exclude_st: bool = True
    max_position_weight: float = Field(default=1.0, gt=0, le=1.0)

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v, info):
        start = info.data.get("start")
        if v is not None and start is not None and v < start:
            raise ValueError("end must not be before start")
        return v


class EquityPoint(BaseModel):
    trade_date: date
    equity: float
    cash: float
    market_value: float
    drawdown: float
    benchmark: float | None = None


class TradeOut(BaseModel):
    code: str
    side: str
    trade_date: date
    price: float
    shares: int
    gross: float
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    slippage: float = 0.0
    pnl: float | None = None
    reason: str = ""


class BacktestResponse(BaseModel):
    run_id: str
    strategy: str
    metrics: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    equity: list[EquityPoint]
    trades: list[TradeOut]
    disclaimer: str = (
        "历史回测不代表未来收益，结果已扣除手续费、印花税、过户费与滑点，"
        "并计入 T+1 与涨跌停限制。仅供研究参考，不构成投资建议。"
    )


class BacktestSummary(BaseModel):
    run_id: str
    strategy: str
    status: str
    created_at: datetime | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    rule_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    scope_type: Literal["all", "index", "watchlist", "codes"] = "watchlist"
    scope_value: str = ""
    cooldown_seconds: int = Field(default=0, ge=0)
    once_per_day: bool = True
    exclude_st: bool = True
    min_amount: float = Field(default=0.0, ge=0)
    enabled: bool = True


class AlertRuleOut(AlertRuleIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime | None = None


class AlertTriggerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_id: int
    code: str
    name: str = ""
    trade_date: date
    trigger_at: datetime | None = None
    rule_type: str = ""
    message: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)
    status: str = "new"


class RuleTypeInfo(BaseModel):
    rule_type: str
    description: str
    params: dict[str, Any]


class TriggerStatusUpdate(BaseModel):
    status: Literal["new", "read", "muted"]


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #


class WatchlistItemIn(BaseModel):
    code: str
    list_name: str = "default"
    note: str = ""


class WatchlistItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    list_name: str
    code: str
    name: str = ""
    note: str = ""


class WatchlistListOut(BaseModel):
    """A derived list: watchlists are grouped by ``list_name``, not a table."""

    list_name: str
    count: int
    last_added_at: datetime | None = None


class WatchlistListRenameIn(BaseModel):
    new_name: str


class WatchlistListRenameOut(BaseModel):
    old_name: str
    new_name: str
    updated: int


class WatchlistListDeleteOut(BaseModel):
    list_name: str
    deleted: int


class WatchlistBatchDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


class WatchlistBatchDeleteOut(BaseModel):
    deleted: int
    missing: list[int] = Field(default_factory=list)


class WatchlistImportInvalid(BaseModel):
    index: int
    code: str
    reason: str
    message: str = ""


class WatchlistQuotaInfo(BaseModel):
    resource: str
    limit: int
    used: int
    remaining: int | None = None


class WatchlistImportIn(BaseModel):
    """Batch add/upsert used by the CSV importer and “选股结果 → 自选”.

    Additive schema: the existing single-item ``POST /watchlist`` contract is
    untouched, so old clients keep working.
    """

    list_name: str
    codes: list[str] = Field(min_length=1, max_length=2000)
    notes: dict[str, str] = Field(default_factory=dict)
    create_list: bool = True
    skip_existing: bool = True


class WatchlistImportOut(BaseModel):
    list_name: str
    added: int
    updated: int
    skipped: int
    invalid: list[WatchlistImportInvalid] = Field(default_factory=list)
    list_created: bool = False
    total_items: int = 0
    quota: WatchlistQuotaInfo | None = None


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #


class ProviderStatus(BaseModel):
    name: str
    circuit_open: bool = False
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    served: int = 0
    last_error: str = ""


class SystemStatus(BaseModel):
    version: str
    timezone: str
    database: dict[str, Any]
    providers: list[ProviderStatus]
    scheduler: dict[str, Any]
    data: dict[str, Any]
    disclaimer: str


class JobResult(BaseModel):
    job: str
    detail: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
