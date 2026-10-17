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
* Authentication tables are deliberately standalone (no FKs from market data), so
  the quant pipeline keeps working unchanged when accounts are switched off.
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

from stockmaster.db.base import Base, utcnow

# Sentinel workspace for rows that belong to nobody in particular.
#
# ``0`` is not a workspace id (ids start at 1), which makes it safe to read as "not
# yet claimed": every row written before accounts existed has it, and single-machine
# installs (``SM_AUTH_ENABLED=false``) keep writing it forever. That keeps the
# un-isolated behaviour byte-identical to the pre-isolation code.
WORKSPACE_UNASSIGNED = 0
# Deprecated alias: 0002 named the column after the *user* it belonged to. 0003
# moved the boundary to workspaces, so new code should use WORKSPACE_UNASSIGNED.
OWNER_UNASSIGNED = WORKSPACE_UNASSIGNED

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
    workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
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

    __table_args__ = (Index("ix_backtest_runs_workspace", "workspace_id", "created_at"),)


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
    workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
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

    __table_args__ = (Index("ix_alert_rules_workspace", "workspace_id", "enabled"),)


class AlertTrigger(Base):
    """A fired alert. ``dedup_key`` + unique constraint make scanning idempotent."""

    __tablename__ = "alert_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Denormalised from the rule on purpose: the history page is the hottest read
    # path and filtering it must not need a join back to ``alert_rules``.
    workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
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
        Index("ix_alert_triggers_workspace", "workspace_id", "trigger_at"),
    )


class WatchlistItem(Base):
    """A named watchlist entry."""

    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
    list_name: Mapped[str] = mapped_column(String(64), default="default")
    code: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(256), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Workspace is part of the key, not just a filter: without it two workspaces
    # holding the same symbol in a list named "default" would collide, and the
    # second ``POST`` would silently overwrite the first workspace's note.
    __table_args__ = (UniqueConstraint("workspace_id", "list_name", "code", name="uq_watchlist_item"),)


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


# --------------------------------------------------------------------------- #
# Workspaces — the isolation boundary for user-generated data
# --------------------------------------------------------------------------- #


class Workspace(Base):
    """A set of data (watchlists, alert rules, backtests) shared by its members.

    Design mirrors cndb's workspace plugin: the workspace — not the account — is the
    unit that data belongs to, and what a member may do inside it is decided by a
    three-level role (owner > editor > viewer).

    ``kind='personal'`` is the workspace every account gets automatically. It is a
    normal workspace in every other respect (it can be shared, renamed, transferred)
    but it cannot be deleted: otherwise a user could erase the only place their data
    lives and leave rows pointing at a vanished workspace.
    """

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(16), default="shared")  # personal | shared
    # No FK cascade: the ``users`` table is created after this one and accounts are
    # never hard-deleted in practice. Orphaned owners are handled by the service.
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_workspaces_owner", "owner_id"),)


class WorkspaceMember(Base):
    """A user's membership — and role — inside one workspace.

    The ``(workspace_id, user_id)`` unique constraint is the whole point: role is a
    property of the *pair*, so "admin of workspace A, viewer of workspace B" is
    expressible without touching the account row.
    """

    __tablename__ = "workspace_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # owner | editor | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
        # Drives "my workspaces" — the query every page load performs.
        Index("ix_workspace_member_user", "user_id", "workspace_id"),
    )


class WorkspaceChannel(Base):
    """工作区级外发通知通道（webhook / Bark）。

    为什么通道要挂在**工作区**而不是账号
    ----------------------------------
    预警规则、触发记录都是工作区的数据，通知自然也该按工作区投递。放在账号上会
    出现「A 的规则触发后，通知发到 B 的手机」；放在全局环境变量上则所有工作区的
    提醒混进同一个 webhook —— 隔离做到最后一步，却在出口处漏掉。

    URL 含凭据
    ----------
    webhook 的 token、Bark 的 device key 都在 URL 里，所以 API **永远只返回掩码**；
    更新时省略 ``url`` 表示保持原值。数据库不加密（与 ``SM_ALERT_WEBHOOK_URL``
    同级的信任边界：能看到库文件的人本来就能看到它），但接口不回显明文。

    ``workspace_id`` 加外键的原因：通道只属于真实工作区，不存在 ``workspace_id=0``
    的「未归属通道」——隔离关闭时的外发仍由全局 ``SM_ALERT_WEBHOOK_URL`` 负责。
    """

    __tablename__ = "workspace_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="webhook")  # webhook | bark
    url: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 审计用：谁建的。账号不硬删除，且通道在隔离关闭时不存在，所以不加外键。
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # 名字是成员识别「这条通道发到哪」的入口；同工作区重名会让停用/删除无从选择。
        UniqueConstraint("workspace_id", "name", name="uq_workspace_channel_name"),
        # 投递时按（工作区, 启用）取通道，是唯一的热查询。
        Index("ix_workspace_channels_workspace", "workspace_id", "enabled"),
    )


# --------------------------------------------------------------------------- #
# Strategy configuration — workspace-scoped presets (选股 / 样本 / 算法)
# --------------------------------------------------------------------------- #


class ScreeningConfig(Base):
    """选股配置：可复用的筛选条件 + 排序 + 条数上限。

    条件集合整体以 JSON 存放（分数阈值、波动/成交额门槛、板块、逐字段条件等），
    因为这些字段会随筛选能力演进增删，而且**从不参与 SQL 过滤**——求值在应用层
    对 ``factors`` 表做，SQL 只负责按 ``workspace_id`` 取本工作区的配置。需要真正
    约束的只有两件事：归属（``workspace_id``）与工作区内重名（唯一约束）。
    """

    __tablename__ = "screening_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    # 审计用：谁建的。不加外键——账号不硬删除，且工作区数据要能在鉴权关闭时独立存在。
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # 名字是用户识别配置的唯一入口；同工作区两条同名配置会让「按名字选」变得
        # 不确定，所以放在数据库约束里，而不是只在服务层靠先查再插（并发会漏）。
        UniqueConstraint("workspace_id", "name", name="uq_screening_config_name"),
        Index("ix_screening_configs_workspace", "workspace_id"),
    )


class SampleConfig(Base):
    """样本配置：训练/评估用哪个标的池、哪段日期、预测多远。

    ``universe_type`` 决定 ``universe_value`` 怎么解释：

    * ``watchlist`` —— 本工作区某个自选清单的名字；
    * ``screening`` —— 某个选股配置的 id（字符串存，求值时解析）；
    * ``codes`` —— 逗号/空白分隔的显式代码；
    * ``default`` —— 交给 ``_default_universe()`` 的默认宇宙（自选/指数成分/全部标的）。

    日期窗口二选一：显式 ``start_date``/``end_date``，或相对最新数据的
    ``lookback_days``。两者都空时用全量历史。
    """

    __tablename__ = "sample_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    universe_type: Mapped[str] = mapped_column(String(16), default="default")
    universe_value: Mapped[str] = mapped_column(String(512), default="")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    lookback_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    horizon: Mapped[int] = mapped_column(Integer, default=5)
    min_bars: Mapped[int] = mapped_column(Integer, default=60)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_sample_config_name"),
        Index("ix_sample_configs_workspace", "workspace_id"),
    )


class AlgorithmConfig(Base):
    """算法配置：用哪个算法、什么超参、在哪个样本上训练。

    ``sample_id`` 可空（空 = 用默认宇宙/默认窗口）；删除样本配置时置空而不是级联
    删除算法配置——算法定义本身仍然有效，只是暂时没有绑定样本。
    """

    __tablename__ = "algorithm_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    algo: Mapped[str] = mapped_column(String(32), default="hist_gbdt")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    sample_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("sample_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    horizon: Mapped[int] = mapped_column(Integer, default=5)
    test_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_algorithm_config_name"),
        Index("ix_algorithm_configs_workspace", "workspace_id"),
    )


# --------------------------------------------------------------------------- #
# Accounts and authentication
# --------------------------------------------------------------------------- #


class User(Base):
    """A local account.

    ``token_version`` is embedded in every issued JWT and compared on each
    request: bumping it invalidates all outstanding tokens for that user, which
    gives stateless tokens a working "log out everywhere" / "password changed"
    semantic without a session table.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # Empty for accounts created purely through WeChat; they can set one later.
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    nickname: Mapped[str] = mapped_column(String(64), default="")
    avatar_url: Mapped[str] = mapped_column(String(512), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | disabled
    token_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_users_role_status", "role", "status"),)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def display_name(self) -> str:
        return self.nickname or self.username


class UserIdentity(Base):
    """A third-party login binding (currently WeChat).

    ``unionid`` is preserved when WeChat supplies it so one person is not split
    into two accounts after the same 开放平台 account authorises both a
    website app and a public account.
    """

    __tablename__ = "user_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="wechat")
    openid: Mapped[str] = mapped_column(String(128))
    unionid: Mapped[str] = mapped_column(String(128), default="")
    nickname: Mapped[str] = mapped_column(String(64), default="")
    avatar_url: Mapped[str] = mapped_column(String(512), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "openid", name="uq_identity_provider_openid"),
        Index("ix_identity_unionid", "provider", "unionid"),
    )


class AuthFlowState(Base):
    """Single-use state for OAuth round-trips and login tickets.

    ``kind='oauth'`` protects the redirect (CSRF/replay) and carries where the
    browser should land afterwards. ``kind='ticket'`` is the short-lived,
    one-time credential handed to the SPA in the URL instead of a real token, so
    access tokens never appear in browser history or referrer headers.
    """

    __tablename__ = "auth_flow_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="oauth")  # oauth | ticket
    provider: Mapped[str] = mapped_column(String(16), default="")
    redirect_to: Mapped[str] = mapped_column(String(512), default="/")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_auth_flow_expires", "expires_at"),)


class AuthAuditLog(Base):
    """Login / account-management trail.

    Kept intentionally small (no request bodies) and prunable by age; it exists so
    that "who changed this account" is answerable without external logging.
    """

    __tablename__ = "auth_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(String(512), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_auth_audit_created", "created_at"),)
