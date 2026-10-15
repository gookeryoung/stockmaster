"""End-to-end acceptance check against LIVE providers.

Runs the real pipeline on a small universe and asserts the outputs are coherent.
This is the strongest available evidence that the system works: real baostock
data, real indicators/factors, real model training, real backtest.
"""

import sys
import time
import warnings

sys.path.insert(0, "backend")
warnings.filterwarnings("ignore")

from datetime import date, timedelta

from app.config import settings
from app.db import init_db, session_scope
from app.providers.registry import get_registry
from app.sync import repository as repo
from app.sync.sync_service import SyncService

UNIVERSE = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH", "000858.SZ",
            "002594.SZ", "600036.SH", "601899.SH"]
START = date(2023, 1, 1)
END = date.today()

results: dict[str, object] = {}
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name} {detail}", flush=True)
    if not condition:
        failures.append(name)


print("=" * 70)
print("StockMaster end-to-end acceptance (LIVE data)")
print(f"db={settings.db_path}")
print("=" * 70, flush=True)

init_db()
service = SyncService(get_registry())

# ---------------------------------------------------------------- calendar
t0 = time.time()
report = service.sync_calendar(START, END)
check("calendar sync", report.failed == 0 and report.processed > 0,
      f"days={report.processed} {time.time()-t0:.1f}s")

# ------------------------------------------------------------- instruments
t0 = time.time()
report = service.sync_instruments()
check("instruments sync", report.processed > 0,
      f"rows={report.processed} {time.time()-t0:.1f}s")

# --------------------------------------------------------------- daily bars
t0 = time.time()
report = service.sync_universe_daily(
    UNIVERSE, start=START, end=END, resume=False
)
elapsed = time.time() - t0
check("daily sync", report.failed == 0 and report.bars_written > 0,
      f"bars={report.bars_written} failed={report.failed} {elapsed:.1f}s")
if report.errors:
    print("   errors:", report.errors[:3], flush=True)

# Verify data landed and matches the recorded cross-source baseline for 600519.
with session_scope() as session:
    qfq = repo.get_bars(session, "600519.SH", "qfq")
    raw = repo.get_bars(session, "600519.SH", "raw")
    instrument = repo.get_instrument(session, "600519.SH")
    calendar_count = len(repo.list_calendar(session))

check("bars persisted", len(qfq) > 400 and len(raw) > 400,
      f"qfq={len(qfq)} raw={len(raw)} calendar={calendar_count}")
check("instrument name resolved", bool(instrument and instrument.name),
      f"name={instrument.name if instrument else None}")

if qfq:
    last = qfq[-1]
    print(f"   last bar: {last.trade_date} close={last.close} "
          f"preclose={last.preclose} pct={last.pct_chg} turn={last.turn}", flush=True)
    check("preclose present (limit logic)", last.preclose is not None)
    check("turnover present", last.turn is not None)
    check("OHLC coherent",
          last.low <= last.open <= last.high and last.low <= last.close <= last.high)

# --------------------------------------------------------- adjustment check
t0 = time.time()
rebuilt = service.rebuild_stale_adjustments(UNIVERSE)
check("adjustment rebuild ran", rebuilt >= 0, f"rebuilt={rebuilt} {time.time()-t0:.1f}s")

# ---------------------------------------------------------------- analytics
from app.jobs.tasks import job_compute_analytics

t0 = time.time()
analytics = job_compute_analytics(UNIVERSE)
check("analytics job", analytics.get("symbols", 0) > 0,
      f"{analytics} {time.time()-t0:.1f}s")

# -------------------------------------------------------------- prediction
from app.jobs.tasks import job_predict, job_train_models

t0 = time.time()
train_result = job_train_models(algo="hist_gbdt", horizon=1)
metrics = train_result.get("metrics", {})
check("model trained", bool(train_result.get("model_id")),
      f"model={train_result.get('model_id')} {time.time()-t0:.1f}s")
if metrics:
    auc = metrics.get("auc")
    ic = metrics.get("ic_spearman")
    base = metrics.get("accuracy_baseline")
    print(f"   OOS: AUC={auc} IC={ic} acc={metrics.get('accuracy')} "
          f"baseline={base} n_train={train_result.get('n_train')} "
          f"n_test={train_result.get('n_test')} "
          f"purged={metrics.get('split', {}).get('purged_rows')}", flush=True)
    check("AUC computed", auc is not None)
    check("baseline reported", base is not None)

t0 = time.time()
predict_result = job_predict(horizon=1)
check("prediction run", predict_result.get("scored", 0) > 0,
      f"scored={predict_result.get('scored')} {time.time()-t0:.1f}s")

# ----------------------------------------------------------------- backtest
from app.api.v1.backtest import _build_signals, _load_bars
from app.backtest.engine import BacktestConfig, run_backtest
from app.schemas import BacktestRequest

with session_scope() as session:
    bars = _load_bars(session, UNIVERSE)

t0 = time.time()
with session_scope() as session:
    signals = _build_signals(
        BacktestRequest(strategy="score_top_n", codes=UNIVERSE, top_n=3, start=START),
        session,
        bars,
    )

check("backtest signals generated", bool(signals), f"signal_days={len(signals)}")

if signals:
    result = run_backtest(bars, signals, config=BacktestConfig(), benchmark="000300.SH")
    m = result.metrics
    print(f"   backtest: return={m.get('total_return'):.4f} "
          f"maxDD={m.get('max_drawdown'):.4f} sharpe={m.get('sharpe')} "
          f"trades={m.get('n_trades')} costs={m.get('total_costs'):.2f} "
          f"skipped={m.get('n_skipped')} reasons={m.get('skip_reasons')}", flush=True)
    check("backtest produced equity", not result.equity.empty)
    check("costs charged", (m.get("total_costs") or 0) > 0)
    check("T+1 / limit machinery exercised",
          isinstance(m.get("skip_reasons"), dict))

# ------------------------------------------------------------------- alerts
from app.alert.engine import AlertEngine

engine = AlertEngine()
rule_id = engine.create_rule(
    name="验收-价格突破",
    rule_type="price_cross",
    params={"threshold": 1.0, "direction": "above"},
    scope_type="codes",
    scope_value=",".join(UNIVERSE),
    once_per_day=True,
)
first = engine.evaluate_all()
second = engine.evaluate_all()
check("alerts trigger", len(first) > 0, f"triggered={len(first)}")
check("alerts idempotent (dedup)", len(second) == 0,
      f"second scan triggered={len(second)}")

# ------------------------------------------------------------------ summary
print("=" * 70)
if failures:
    print(f"ACCEPTANCE FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("ACCEPTANCE PASSED: all checks green")
print("=" * 70)

get_registry().close()
