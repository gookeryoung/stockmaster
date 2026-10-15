"""ORM models for StockMaster.

Design notes
------------
* ``daily_bars`` keeps an immutable **raw** (不复权) series plus a materialised
  **qfq** series. Forward-adjusted prices change retroactively whenever a new
  dividend/split occurs, so ``qfq`` is rebuilt from ``raw`` + ``adjust_factors``
  rather than appended to. ``hfq`` is derived on demand and never stored.
* ``raw`` rows carry ``preclose``/``is_st`` snapshots because price-limit rules
  and ST status are point-in-time facts.
* Alert delivery is deduplicated by a unique ``(rule_id, dedup_key)`` constraint,
  which makes evaluation idempotent no matter how often the scanner runs.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #


class Instrument(Base):
    """Security master (stocks and indices)."""

    __tablename__ = "instruments"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # 600519.SH
    name: Mapped[str] = mapped_column(String(64), default="")
    market: Mapped[str] = mapped_column(String(8), default="")  # SH/SZ/BJ
    board: Mapped[str] = mapped_column(String(16), default="")  # main/star/chinext/bse/index
    industry: Mapped[str] = mapped_column(String(64), default="")
    is_index: Mapped[bool] = mapped_column(Boolean, default=False)
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[int] = mapped_column(Integer, default=1)  # 1 listed, 0 delisted
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_instruments_name", "name"),
        Index("ix_instruments_status", "status", "is_index"),
    )


class TradeCalendar(Base):
    """One row per calendar day; ``is_open`` marks trading days."""

    __tablename__ = "trade_calendar"

    calendar_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IndexConstituent(Base):
    """Index membership (point-in-time snapshots keyed by as-of date)."""

    __tablename__ = "index_constituents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_code: Mapped[str] = mapped_column(String(16), index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(16), default="")

    __table_args__ = (UniqueConstraint("index_code", "code", "as_of", name="uq_index_cons"),)


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class DailyBar(Base):
    """Daily OHLCV for one (code, date, adjust) triple."""

    __tablename__ = "daily_bars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    # raw | qfq  (hfq intentionally not persisted)
    adjust: Mapped[str] = mapped_column(String(4), nullable=False, default="raw")

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    preclose: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    turn: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_chg: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb_mrq: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    # True when the session never left the limit price (一字板) — untradeable both ways.
    one_word_limit: Mapped[bool] = mapped_column(Boolean, default=False)

    source: Mapped[str] = mapped_column(String(16), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("code", "trade_date", "adjust", name="uq_bar_code_date_adjust"),
        Index("ix_bars_code_adjust_date", "code", "adjust", "trade_date"),
        Index("ix_bars_date_adjust", "trade_date", "adjust"),
    )


class AdjustFactor(Base):
    """Cumulative adjustment factors per ex-date (basis for qfq/hfq rebuilds)."""

    __tablename__ = "adjust_factors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    qfq_factor: Mapped[float] = mapped_column(Float, default=1.0)
    hfq_factor: Mapped[float] = mapped_column(Float, default=1.0)
    # cash dividend per share and split/bonus ratio, when known
    cash_div: Mapped[float | None] = mapped_column(Float, nullable=True)
    bonus_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("code", "ex_date", name="uq_adjust_code_exdate"),
        Index("ix_adjust_code", "code"),
    )


# --------------------------------------------------------------------------- #
# Derived analytics
# --------------------------------------------------------------------------- #


class Indicator(Base):
    """Latest computed technical indicators per (code, date)."""

    __tablename__ = "indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)

    ma5: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma60: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma120: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema12: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema26: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_dif: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_dea: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_hist: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi6: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi12: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi24: Mapped[float | None] = mapped_column(Float, nullable=True)
    kdj_k: Mapped[float | None] = mapped_column(Float, nullable=True)
    kdj_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    kdj_j: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_up: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_mid: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr14: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    obv: Mapped[float | None] = mapped_column(Float, nullable=True)
    vol_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_indicator_code_date"),
        Index("ix_indicators_date", "trade_date"),
    )


class Factor(Base):
    """Cross-sectional factors plus their daily standardised ranks."""

    __tablename__ = "factors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)

    mom_5: Mapped[float | None] = mapped_column(Float, nullable=True)
    mom_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    mom_60: Mapped[float | None] = mapped_column(Float, nullable=True)
    mom_120: Mapped[float | None] = mapped_column(Float, nullable=True)
    dist_high_252: Mapped[float | None] = mapped_column(Float, nullable=True)
    vol_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    vol_60: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_log_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    amihud_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma_bull: Mapped[float | None] = mapped_column(Float, nullable=True)
    breakout_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi_14: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Cross-sectional transforms (computed within each trade_date)
    z_mom_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    z_mom_60: Mapped[float | None] = mapped_column(Float, nullable=True)
    z_vol_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    z_turnover_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    z_rsi_14: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_mom_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_mom_60: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_factor_code_date"),
        Index("ix_factors_date", "trade_date"),
        Index("ix_factors_date_score", "trade_date", "score"),
    )


# --------------------------------------------------------------------------- #
# Prediction
# --------------------------------------------------------------------------- #


class ModelRegistry(Base):
    """Trained model artefacts and their honest out-of-sample metrics."""

    __tablename__ = "model_registry"

    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    algo: Mapped[str] = mapped_column(String(32))
    horizon: Mapped[int] = mapped_column(Integer, default=1)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    features_json: Mapped[str] = mapped_column(Text, default="[]")
    train_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    train_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    test_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    test_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    importance_json: Mapped[str] = mapped_column(Text, default="{}")
    n_train: Mapped[int] = mapped_column(Integer, default=0)
    n_test: Mapped[int] = mapped_column(Integer, default=0)
    artifact_path: Mapped[str] = mapped_column(String(512), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    trained_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_model_active", "is_active", "horizon"),)


class Prediction(Base):
    """Per-instrument forward-looking predictions."""

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon: Mapped[int] = mapped_column(Integer, default=1)

    prob_up: Mapped[float | None] = mapped_column(Float, nullable=True)
    exp_ret: Mapped[float | None] = mapped_column(Float, nullable=True)
    interval_lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    interval_hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("model_id", "code", "trade_date", "horizon", name="uq_pred_key"),
        Index("ix_predictions_date_horizon", "trade_date", "horizon"),
    )


# --------------------------------------------------------------------------- #
# Backtesting
# --------------------------------------------------------------------------- #


class BacktestRun(Base):
    """One backtest execution and its summary metrics."""

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    name: Mapped[str] = mapped_column(String(128), default="")
    strategy: Mapped[str] = mapped_column(String(64))
    universe: Mapped[str] = mapped_column(String(64), default="")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    initial_cash: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    cost_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BacktestEquity(Base):
    """Daily equity-curve point for a backtest."""

    __tablename__ = "backtest_equity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("backtest_runs.id", ondelete="CASCADE"))
    trade_date: Mapped[date] = mapped_column(Date)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    benchmark: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "trade_date", name="uq_equity_run_date"),
        Index("ix_equity_run", "run_id"),
    )


class BacktestTrade(Base):
    """Executed backtest transaction."""

    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("backtest_runs.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    trade_date: Mapped[date] = mapped_column(Date)
    price: Mapped[float] = mapped_column(Float)
    shares: Mapped[int] = mapped_column(Integer)
    gross: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    stamp_tax: Mapped[float] = mapped_column(Float, default=0.0)
    transfer_fee: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (
        Index("ix_trades_run", "run_id"),
        Index("ix_trades_run_date", "run_id", "trade_date"),
    )


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


class AlertRule(Base):
    """User-defined alert rule."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    rule_type: Mapped[str] = mapped_column(String(32))
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    scope_type: Mapped[str] = mapped_column(String(16), default="watchlist")  # all|index|watchlist|codes
    scope_value: Mapped[str] = mapped_column(String(256), default="")
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0)
    once_per_day: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exclude_st: Mapped[bool] = mapped_column(Boolean, default=True)
    min_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AlertTrigger(Base):
    """A fired alert. ``dedup_key`` + unique constraint make scanning idempotent."""

    __tablename__ = "alert_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, ForeignKey("alert_rules.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64), default="")
    trade_date: Mapped[date] = mapped_column(Date)
    trigger_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    rule_type: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    dedup_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="new")  # new|read|muted
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("rule_id", "dedup_key", name="uq_trigger_rule_dedup"),
        Index("ix_triggers_date", "trade_date"),
        Index("ix_triggers_status", "status"),
    )


class WatchlistItem(Base):
    """A named watchlist entry."""

    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    list_name: Mapped[str] = mapped_column(String(64), default="default")
    code: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(256), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("list_name", "code", name="uq_watchlist_item"),)


# --------------------------------------------------------------------------- #
# Operational
# --------------------------------------------------------------------------- #


class DataSyncTask(Base):
    """Resumable sync bookkeeping (one row per job+target)."""

    __tablename__ = "data_sync_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|done|failed
    cursor: Mapped[str] = mapped_column(String(256), default="")
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("job", "target", name="uq_synctask_job_target"),
        Index("ix_synctask_status", "status"),
    )


class DataSyncError(Base):
    """Per-symbol sync failure log."""

    __tablename__ = "data_sync_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(16), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_syncerr_job", "job", "code"),)
