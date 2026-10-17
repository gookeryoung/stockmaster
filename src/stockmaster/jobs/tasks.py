"""Scheduled jobs.

The daily pipeline is intentionally sequenced rather than parallel: bars must
land before indicators, indicators before factors, factors before predictions,
and predictions before the alert scan. Each job is independently runnable via the
API, so a user can re-run one stage without repeating the whole chain.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from time import monotonic

from stockmaster.core.logging import get_logger
from stockmaster.core.market import dashboard_index_codes
from stockmaster.db.models import DataSyncTask, Prediction
from stockmaster.db.session import session_scope
from stockmaster.plugins.alerts.services.channels import Notification, notify
from stockmaster.plugins.alerts.services.engine import AlertEngine
from stockmaster.plugins.predict.services.train import load_active_model, score_symbols
from stockmaster.sync import repository as repo
from stockmaster.sync.sync_service import get_sync_service

log = get_logger(__name__)

__all__ = [
    "job_compute_analytics",
    "job_predict",
    "job_scan_alerts",
    "job_snapshot_poll",
    "job_sync_calendar",
    "job_sync_daily",
    "job_sync_instruments",
    "job_train_models",
    "run_pipeline",
]


# --------------------------------------------------------------------------- #
# Data jobs
# --------------------------------------------------------------------------- #


def job_sync_calendar(lookahead_days: int = 400) -> dict:
    """Refresh the trading calendar."""
    service = get_sync_service()
    today = date.today()
    report = service.sync_calendar(today - timedelta(days=30), today + timedelta(days=lookahead_days))
    return report.as_dict()


def job_sync_instruments() -> dict:
    """Refresh the security master and index membership."""
    service = get_sync_service()
    report = service.sync_instruments()
    index_reports = []
    for index_code in ("000300.SH", "000905.SH"):
        sub = service.sync_index_members(index_code)
        index_reports.append(sub.as_dict())
    payload = report.as_dict()
    payload["indices"] = index_reports
    return payload


def job_sync_daily(full: bool = False, codes: list[str] | None = None) -> dict:
    """Incrementally sync daily bars for a universe (watchlist or core pool)."""
    service = get_sync_service()
    if codes is None:
        codes = _default_universe()
    if not codes:
        return {"job": "daily", "processed": 0, "message": "no universe available"}

    started = monotonic()
    report = service.sync_universe_daily(codes, end=date.today(), resume=not full)
    payload = report.as_dict()
    payload["elapsed_seconds"] = round(monotonic() - started, 2)
    return payload


def _default_universe(workspace_id: int | None = None) -> list[str]:
    """The set of symbols the daily pipeline processes.

    Preference order: explicit watchlist, then index members, then all known
    instruments — always restricted to symbols that actually have stored bars.

    ``workspace_id=None`` means "every workspace's watchlists, merged". That is
    what the scheduled jobs want: market data is shared, so syncing the union once
    is both cheaper and more correct than keeping one copy per workspace. Passing
    an id narrows it to a single workspace, which is what the backtest API uses
    when isolation is on.

    That last constraint matters: a cloud-sourced index-constituent list can
    contain codes that were never synced (or were mis-parsed). Returning them
    would make every downstream job find "no data" and fail with a confusing
    message instead of falling back to a usable universe.
    """
    with session_scope() as session:
        from sqlalchemy import select

        from stockmaster.db.models import DailyBar, Instrument, WatchlistItem

        def with_bars(candidates: list[str]) -> list[str]:
            if not candidates:
                return []
            stored = set(
                session.execute(select(DailyBar.code).where(DailyBar.code.in_(candidates)).distinct()).scalars().all()
            )
            return [c for c in candidates if c in stored]

        watched_stmt = select(WatchlistItem.code)
        if workspace_id is not None:
            watched_stmt = watched_stmt.where(WatchlistItem.workspace_id == workspace_id)
        # 并集去重：多个工作区把同一只股票加进自选是常态，重复的 code 会让下游
        # 同步任务对同一个标的跑两遍。
        watched = with_bars(list(session.execute(watched_stmt.distinct()).scalars().all()))
        if watched:
            return watched

        members = with_bars(repo.index_members(session, "000300.SH"))
        if members:
            return members

        known = with_bars(
            list(
                session.execute(select(Instrument.code).where(Instrument.status == 1, Instrument.is_index.is_(False)))
                .scalars()
                .all()
            )
        )
        return known


def job_sync_index_daily(index_codes: tuple[str, ...] | None = None) -> dict:
    """Sync index daily bars for every index the dashboard shows.

    Defaults to `DASHBOARD_INDICES` rather than a hand-written subset, so a card can
    never be empty simply because the job's list was shorter than the page's.
    """
    if index_codes is None:
        index_codes = dashboard_index_codes()

    service = get_sync_service()
    written = 0
    errors: list[str] = []
    for code in index_codes:
        with session_scope() as session:
            last = repo.last_bar_date(session, code, "raw")
        start = (last - timedelta(days=1)) if last else date.today() - timedelta(days=365 * 5)
        try:
            written += service.sync_index_daily(code, start, date.today())
        except Exception as exc:
            errors.append(f"{code}: {exc}")
            continue
    return {"job": "index_daily", "bars_written": written, "errors": errors}


# --------------------------------------------------------------------------- #
# Analytics jobs
# --------------------------------------------------------------------------- #


def job_compute_analytics(codes: list[str] | None = None) -> dict:
    """Compute and store indicators and factors for a universe.

    Both are computed from each symbol's own history (point-in-time). The
    cross-sectional standardisation is applied per trade_date across whatever
    universe is present, so it should be re-run after a universe change.
    """
    import pandas as pd

    from stockmaster.factors.compute import composite_score as factor_score
    from stockmaster.factors.compute import compute_factors, standardize_cross_section
    from stockmaster.indicators.compute import compute_indicators

    codes = codes or _default_universe()
    started = monotonic()
    indicator_rows: list[dict] = []
    factor_rows: list[dict] = []

    with session_scope() as session:
        for code in codes:
            bars = repo.get_bars(session, code, "qfq")
            if len(bars) < 30:
                continue
            frame = pd.DataFrame(
                {
                    "trade_date": [b.trade_date for b in bars],
                    "open": [b.open for b in bars],
                    "high": [b.high for b in bars],
                    "low": [b.low for b in bars],
                    "close": [b.close for b in bars],
                    "volume": [b.volume for b in bars],
                    "amount": [b.amount for b in bars],
                    "turn": [b.turn if b.turn is not None else float("nan") for b in bars],
                }
            )
            indicators = compute_indicators(frame)
            factors = compute_factors(frame)

            # Persist only the most recent row per symbol to keep the store small;
            # historical series are recomputed on demand.
            last_date = frame["trade_date"].iloc[-1]
            indicator_rows.append(
                {
                    "code": code,
                    "trade_date": last_date,
                    **{column: _clean(indicators[column].iloc[-1]) for column in indicators.columns},
                }
            )
            factor_rows.append(
                {
                    "code": code,
                    "trade_date": last_date,
                    **{column: _clean(factors[column].iloc[-1]) for column in factors.columns},
                }
            )

    if not factor_rows:
        return {
            "job": "analytics",
            "symbols": 0,
            "message": "no symbols had sufficient history",
        }

    # Cross-sectional transforms need a whole date at a time.
    factors_frame = pd.DataFrame(factor_rows)
    factors_frame = standardize_cross_section(factors_frame)
    factors_frame["score"] = factor_score(factors_frame)

    with session_scope() as session:
        _upsert_indicators(session, indicator_rows)
        _upsert_factors(session, factors_frame.to_dict("records"))

    return {
        "job": "analytics",
        "symbols": len(factor_rows),
        "indicators": len(indicator_rows),
        "factors": len(factors_frame),
        "elapsed_seconds": round(monotonic() - started, 2),
    }


def _clean(value):
    import pandas as pd

    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _upsert_indicators(session, rows: list[dict]) -> None:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from stockmaster.db.models import Indicator

    keys = [
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "ma120",
        "ema12",
        "ema26",
        "macd_dif",
        "macd_dea",
        "macd_hist",
        "rsi6",
        "rsi12",
        "rsi24",
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "boll_up",
        "boll_mid",
        "boll_low",
        "atr14",
        "atr_pct",
        "obv",
        "vol_ratio",
    ]
    payload = [{"code": r["code"], "trade_date": r["trade_date"], **{k: r.get(k) for k in keys}} for r in rows]
    stmt = sqlite_insert(Indicator).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Indicator.code, Indicator.trade_date],
        set_={k: getattr(stmt.excluded, k) for k in keys},
    )
    session.execute(stmt)


def _upsert_factors(session, rows: list[dict]) -> None:
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from stockmaster.db.models import Factor

    keys = [
        "mom_5",
        "mom_20",
        "mom_60",
        "mom_120",
        "dist_high_252",
        "vol_20",
        "vol_60",
        "atr_pct",
        "turnover_20",
        "amount_log_20",
        "amihud_20",
        "ma_bull",
        "breakout_20",
        "rsi_14",
        "z_mom_20",
        "z_mom_60",
        "z_vol_20",
        "z_turnover_20",
        "z_rsi_14",
        "pct_mom_20",
        "pct_mom_60",
        "score",
    ]
    payload = [{"code": r["code"], "trade_date": r["trade_date"], **{k: _clean(r.get(k)) for k in keys}} for r in rows]
    if not payload:
        return
    stmt = sqlite_insert(Factor).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Factor.code, Factor.trade_date],
        set_={k: getattr(stmt.excluded, k) for k in keys},
    )
    session.execute(stmt)


# --------------------------------------------------------------------------- #
# Prediction jobs
# --------------------------------------------------------------------------- #


def job_train_models(algo: str = "hist_gbdt", horizon: int | None = None) -> dict:
    """Train (or refresh) the prediction model(s)."""
    from stockmaster.plugins.predict.services.dataset import build_dataset, load_bars_by_symbol
    from stockmaster.plugins.predict.services.train import train_all_horizons, train_model

    codes = _default_universe()
    with session_scope() as session:
        bars = load_bars_by_symbol(session, codes, adjust="qfq")
    if not bars:
        return {"job": "train", "message": "no data available to train on"}

    if horizon is not None:
        dataset = build_dataset(bars, horizon=horizon)
        model = train_model(dataset, algo=algo, persist=True)
        return {
            "job": "train",
            "model_id": model.model_id,
            "horizon": horizon,
            "metrics": model.metrics,
            "n_train": model.n_train,
            "n_test": model.n_test,
        }

    models = train_all_horizons(bars, algo=algo, persist=True)
    return {
        "job": "train",
        "models": {
            str(h): {"model_id": m.model_id, "auc": m.metrics.get("auc"), "ic": m.metrics.get("ic_spearman")}
            for h, m in models.items()
        },
    }


def job_predict(horizon: int = 1) -> dict:
    """Score the universe with the active model for a horizon."""
    from stockmaster.plugins.predict.services.dataset import load_bars_by_symbol

    try:
        model = load_active_model(horizon=horizon)
    except Exception as exc:
        return {"job": "predict", "message": f"no active model: {exc}"}

    codes = _default_universe()
    with session_scope() as session:
        bars = load_bars_by_symbol(session, codes, adjust="qfq")
    records = score_symbols(model, bars, persist=True)
    return {
        "job": "predict",
        "model_id": model.model_id,
        "horizon": horizon,
        "scored": len(records),
    }


# --------------------------------------------------------------------------- #
# Alert jobs
# --------------------------------------------------------------------------- #


def job_scan_alerts(notify_channels: bool = True) -> dict:
    """Evaluate alert rules and deliver new triggers."""
    engine = AlertEngine()
    candidates = engine.evaluate_all()
    if not candidates:
        return {"job": "alerts", "triggered": 0}

    if notify_channels:
        results = notify(
            [
                Notification(
                    title=f"{c.name or c.code} {c.rule_type}",
                    body=c.message,
                    code=c.code,
                    rule_id=c.rule_id,
                )
                for c in candidates
            ]
        )
    else:
        results = {}
    return {
        "job": "alerts",
        "triggered": len(candidates),
        "delivered": results,
        "codes": sorted({c.code for c in candidates})[:50],
    }


def job_snapshot_poll() -> dict:
    """Light intraday snapshot poll for watchlist names (alert support)."""
    from stockmaster.providers.registry import get_registry

    codes = _default_universe()
    if not codes:
        return {"job": "snapshot", "updated": 0}
    try:
        snapshots = get_registry().fetch_snapshot(codes[:200])
    except Exception as exc:
        return {"job": "snapshot", "error": str(exc)[:200]}
    return {"job": "snapshot", "updated": len(snapshots)}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run_pipeline(*, train: bool = False, predict: bool = True) -> dict:
    """Run the full daily chain in dependency order.

    Failures in one stage are recorded but do not abort the chain, so a provider
    hiccup on indicators cannot prevent alerts from being scanned.
    """
    started = monotonic()
    results: dict[str, object] = {}
    steps = [
        ("calendar", job_sync_calendar),
        ("instruments", job_sync_instruments),
        ("index_daily", job_sync_index_daily),
        ("daily", job_sync_daily),
        ("analytics", job_compute_analytics),
    ]
    if train:
        steps.append(("train", job_train_models))
    if predict:
        steps.append(("predict", job_predict))
    steps.append(("alerts", job_scan_alerts))

    for name, func in steps:
        try:
            results[name] = func()
        except Exception as exc:
            log.exception("pipeline stage %s failed", name)
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}

    results["elapsed_seconds"] = round(monotonic() - started, 2)
    return results


def record_task_status(job: str, status: str, message: str = "") -> None:
    """Update the observability row for a background job."""
    with session_scope() as session:
        from sqlalchemy import select

        task = session.execute(select(DataSyncTask).where(DataSyncTask.job == job)).scalar_one_or_none()
        if task is None:
            task = DataSyncTask(job=job, target="job")
            session.add(task)
        task.status = status
        task.message = message[:2000]
        task.finished_at = datetime.now()


def recent_predictions(limit: int = 50) -> list[dict]:
    """Latest stored predictions, for the dashboard."""
    with session_scope() as session:
        rows = (
            session.execute(
                __import__("sqlalchemy")
                .select(Prediction)
                .order_by(Prediction.trade_date.desc(), Prediction.prob_up.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
    return [
        {
            "code": r.code,
            "trade_date": str(r.trade_date),
            "horizon": r.horizon,
            "prob_up": r.prob_up,
            "model_id": r.model_id,
        }
        for r in rows
    ]
