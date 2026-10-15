"""Command-line interface for operations that do not belong in an HTTP request.

Long-running work (full-market backfill) is exposed here rather than as an API
endpoint: a multi-hour crawl behind a synchronous HTTP call is a poor design, and
this path supports checkpointed resume.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from app import __version__
from app.core.console import prefer_console_encoding
from app.core.logging import get_logger, setup_logging

log = get_logger(__name__)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("--years", type=int, default=5, help="历史年数")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockmaster",
        description="StockMaster A股日线量化分析工作台 CLI",
    )
    parser.add_argument("--version", action="version", version=f"StockMaster {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化数据库")
    _add_common(init)

    calendar = sub.add_parser("sync-calendar", help="同步交易日历")
    _add_common(calendar)

    instruments = sub.add_parser("sync-instruments", help="同步证券主数据")
    _add_common(instruments)

    daily = sub.add_parser("sync-daily", help="同步日线（默认核心池，可用 --full 全市场）")
    _add_common(daily)
    daily.add_argument("--codes", default="", help="逗号分隔的代码；留空表示默认池")
    daily.add_argument("--full", action="store_true", help="全市场回补")
    daily.add_argument("--resume", action="store_true", default=True, help="断点续传（默认开启）")
    daily.add_argument("--workers", type=int, default=4, help="并发取数线程数")

    analytics = sub.add_parser("analytics", help="计算指标与因子")
    _add_common(analytics)
    analytics.add_argument("--codes", default="")

    train = sub.add_parser("train", help="训练预测模型")
    _add_common(train)
    train.add_argument("--algo", default="hist_gbdt")
    train.add_argument("--horizon", type=int, default=1)

    predict = sub.add_parser("predict", help="运行预测")
    _add_common(predict)
    predict.add_argument("--horizon", type=int, default=1)

    pipeline = sub.add_parser("pipeline", help="运行完整日常流水线")
    _add_common(pipeline)

    serve = sub.add_parser("serve", help="启动 API 服务")
    _add_common(serve)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")

    stats = sub.add_parser("stats", help="显示数据概况")
    _add_common(stats)

    doctor = sub.add_parser("doctor", help="环境自检（依赖、编码、配置、数据目录）")
    _add_common(doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    prefer_console_encoding()
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "log_level", "INFO"))

    from app.db import init_db

    init_db()

    command = args.command

    if command == "init":
        from app.config import settings

        print(f"数据库已初始化：{settings.db_path}")
        return 0

    if command == "stats":
        return _stats()

    if command == "doctor":
        return _doctor()

    if command == "sync-calendar":
        from app.jobs.tasks import job_sync_calendar

        print(job_sync_calendar())
        return 0

    if command == "sync-instruments":
        from app.jobs.tasks import job_sync_instruments

        print(job_sync_instruments())
        return 0

    if command == "sync-daily":
        return _sync_daily(args)

    if command == "analytics":
        from app.jobs.tasks import job_compute_analytics

        codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
        print(job_compute_analytics(codes))
        return 0

    if command == "train":
        from app.jobs.tasks import job_train_models

        print(job_train_models(algo=args.algo, horizon=args.horizon))
        return 0

    if command == "predict":
        from app.jobs.tasks import job_predict

        print(job_predict(horizon=args.horizon))
        return 0

    if command == "pipeline":
        from app.jobs.tasks import run_pipeline

        print(run_pipeline())
        return 0

    if command == "serve":
        return _serve(args)

    print(f"未知命令：{command}", file=sys.stderr)
    return 2


def _doctor() -> int:
    """Environment self-check.

    Reports the resolved console encoding because a wrong code page is the single
    most confusing failure mode on Windows: everything works yet the Chinese
    labels render as mojibake.
    """
    import platform

    from app.config import REPO_ROOT, settings
    from app.core.console import detect_console_encoding, locale_encoding

    print("StockMaster 环境自检")
    print(f"  Python       : {platform.python_version()} ({platform.system()} {platform.release()})")
    print(f"  项目根目录   : {REPO_ROOT}")
    print(f"  数据目录     : {settings.data_path}  (存在: {settings.data_path.exists()})")
    print(f"  数据库       : {settings.db_path}  (存在: {settings.db_path.exists()})")
    print(f"  控制台编码   : {detect_console_encoding()}  (进程 locale: {locale_encoding()})")
    if settings.console_encoding_override:
        print(f"  编码覆盖     : SM_CONSOLE_ENCODING={settings.console_encoding_override}")
    print(f"  调度器       : {'启用' if settings.scheduler_enabled else '禁用'}")
    print(f"  数据源优先级 : {', '.join(settings.provider_priority)}")
    print(f"  超时/重试    : {settings.provider_timeout}s / {settings.provider_max_retries} 次")

    missing: list[str] = []
    for module in ("fastapi", "sqlalchemy", "pandas", "numpy", "sklearn", "baostock", "akshare", "apscheduler"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        print(f"  [警告] 缺少依赖：{', '.join(missing)}（执行 make sync）")
        return 1

    print("  依赖检查     : 全部可用")
    return 0


def _stats() -> int:
    from sqlalchemy import func, select

    from app.config import settings
    from app.db.models import DailyBar, Instrument, ModelRegistry, Prediction
    from app.db.session import session_scope

    with session_scope() as session:
        bars = session.execute(select(func.count()).select_from(DailyBar)).scalar() or 0
        instruments = session.execute(select(func.count()).select_from(Instrument)).scalar() or 0
        latest = session.execute(select(func.max(DailyBar.trade_date))).scalar()
        models = (
            session.execute(
                select(func.count()).select_from(ModelRegistry).where(ModelRegistry.is_active.is_(True))
            ).scalar()
            or 0
        )
        predictions = session.execute(select(func.count()).select_from(Prediction)).scalar() or 0

    print(f"数据库           : {settings.db_path}")
    print(f"证券数量         : {instruments}")
    print(f"日线行数         : {bars}")
    print(f"最新行情日       : {latest or '无'}")
    print(f"生效模型数       : {models}")
    print(f"预测记录数       : {predictions}")
    return 0


def _sync_daily(args) -> int:
    """Sync daily bars, choosing the core pool or the whole market.

    Modes:
      * ``--codes``   explicit list
      * ``--full``    every listed instrument from the security master
      * default       the core pool (watchlist -> index members -> known codes)

    Checkpointing is on by default, so an interrupted run continues where it
    stopped instead of re-fetching everything.
    """
    from sqlalchemy import select

    from app.db.models import Instrument
    from app.db.session import session_scope
    from app.providers.registry import get_registry
    from app.sync.sync_service import SyncService

    codes = [c.strip() for c in getattr(args, "codes", "").split(",") if c.strip()]

    with session_scope() as session:
        if not codes and args.full:
            codes = list(
                session.execute(
                    select(Instrument.code).where(Instrument.status == 1, Instrument.is_index.is_(False))
                ).scalars()
            )
        targets = codes or SyncService(get_registry()).core_universe()

    if not targets:
        print(
            "没有可同步的标的。请先运行：stockmaster sync-instruments",
            file=sys.stderr,
        )
        return 1

    end = date.today()
    start = date(end.year - args.years, end.month, end.day)

    print(f"准备同步 {len(targets)} 只标的，区间 {start} ~ {end}")
    print("（全市场回补耗时较长，可随时中断后用同样命令续传）")

    service = SyncService(get_registry())
    report = service.sync_universe_daily(
        targets,
        start=start,
        end=end,
        workers=args.workers,
        resume=args.resume,
    )
    payload = report.as_dict()
    print(
        f"完成：处理 {payload['processed']} / {payload['total']}，"
        f"写入 {payload['bars_written']} 根日线，失败 {payload['failed']}"
    )
    if payload["errors"]:
        print("部分错误：")
        for message in payload["errors"][:10]:
            print("  -", message)
    get_registry().close()
    return 0 if payload["failed"] == 0 else 1


def _serve(args) -> int:
    import uvicorn

    from app.config import settings

    host = args.host or settings.host
    port = args.port or settings.port
    print(f"StockMaster 启动于 http://{host}:{port}")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=bool(args.reload),
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
