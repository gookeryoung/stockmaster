"""Command-line interface for operations that do not belong in an HTTP request.

Long-running work (full-market backfill) is exposed here rather than as an API
endpoint: a multi-hour crawl behind a synchronous HTTP call is a poor design, and
this path supports checkpointed resume.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from datetime import date
from pathlib import Path

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
    daily.add_argument("--retries", type=int, default=2, help="失败标的的重试轮数")

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

    parser_dev = sub.add_parser("dev", help="开发模式：同一控制台内同时运行后端与前端")
    _add_common(parser_dev)
    parser_dev.add_argument("--host", default=None, help="绑定地址（默认 127.0.0.1）")
    parser_dev.add_argument("--port", type=int, default=None, help="后端端口（默认 8770）")
    parser_dev.add_argument("--frontend-port", type=int, default=None, help="前端端口（默认 5173）")
    parser_dev.add_argument("--no-frontend", action="store_true", help="只启动后端")
    parser_dev.add_argument("--no-reload", action="store_true", help="关闭后端热重载")

    stats = sub.add_parser("stats", help="显示数据概况")
    _add_common(stats)

    doctor = sub.add_parser("doctor", help="环境自检（依赖、编码、配置、数据目录）")
    _add_common(doctor)

    quality = sub.add_parser("data-quality", help="数据质量体检（缺口/异常/陈旧/复权一致性）")
    _add_common(quality)
    quality.add_argument("--codes", default="", help="逗号分隔的代码；留空表示按最近更新抽样")
    quality.add_argument("--adjust", default="qfq", choices=["qfq", "raw"], help="检查哪一套序列")
    quality.add_argument("--max-symbols", type=int, default=200, help="逐只检查的标的上限")
    quality.add_argument("--stale-days", type=int, default=5, help="落后多少天算陈旧")
    quality.add_argument(
        "--min-usable-share", type=float, default=0.0, help="可用标的占比低于该值时以非零码退出（可用于门禁，如 0.5）"
    )

    # --- 账号管理 ---
    account = sub.add_parser("user", help="账号管理（创建、列表、重置密码、改角色）")
    _add_common(account)
    account_sub = account.add_subparsers(dest="user_command", required=True)

    user_add = account_sub.add_parser("add", help="创建账号（第二个及以后的账号默认 user 角色）")
    user_add.add_argument("--username", required=True)
    user_add.add_argument("--password", default="", help="留空则随机生成并打印")
    user_add.add_argument("--email", default="")
    user_add.add_argument("--nickname", default="")
    user_add.add_argument("--role", default="user", choices=["admin", "user"])
    user_add.add_argument("--status", default="active", choices=["active", "disabled"])

    account_sub.add_parser("list", help="列出账号")

    user_passwd = account_sub.add_parser("passwd", help="重置密码（留空则随机生成并打印）")
    user_passwd.add_argument("--username", required=True)
    user_passwd.add_argument("--password", default="")

    user_role = account_sub.add_parser("role", help="修改角色或启用状态")
    user_role.add_argument("--username", required=True)
    user_role.add_argument("--role", default=None, choices=["admin", "user"])
    user_role.add_argument("--status", default=None, choices=["active", "disabled"])

    user_delete = account_sub.add_parser("delete", help="删除账号")
    user_delete.add_argument("--username", required=True)

    user_unbind = account_sub.add_parser("unbind-wechat", help="解绑账号下的全部微信")
    user_unbind.add_argument("--username", required=True)

    # --- 数据库迁移 ---
    db = sub.add_parser("db", help="数据库迁移（Alembic）")
    _add_common(db)
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("current", help="显示当前迁移版本与是否最新")
    db_sub.add_parser("history", help="列出迁移链及已应用情况")
    db_sub.add_parser("upgrade", help="建表并升级到最新版本（等价于启动时的自动迁移）")
    db_stamp = db_sub.add_parser("stamp", help="只写版本标记、不执行迁移（认领 create_all 时代的存量库）")
    db_stamp.add_argument("--revision", default="head", help="目标版本，默认 head")
    db_sub.add_parser("check", help="比对 ORM 模型与库结构，报告缺表/缺列")

    # --- 备份与恢复 ---
    backup = sub.add_parser("backup", help="创建应用级备份（tar.gz + manifest）")
    _add_common(backup)
    backup.add_argument("-o", "--output", default="", help="输出路径；缺省落到 <数据目录>/backups/")
    backup.add_argument("--no-models", action="store_true", help="不包含模型产物（*.joblib）")
    backup.add_argument("--no-secret", action="store_true", help="不包含令牌签名密钥（恢复后所有人需重新登录）")
    backup.add_argument("--note", default="", help="写进 manifest 的备注")
    backup.add_argument("--list", dest="list_only", action="store_true", help="只列出本地已有备份")

    restore = sub.add_parser("restore", help="从备份恢复（破坏性，需 --force 才会覆盖非空库）")
    _add_common(restore)
    restore.add_argument("archive", help="备份归档路径（.tar.gz）")
    restore.add_argument("--force", action="store_true", help="目标库非空时必须显式指定")
    restore.add_argument("--dry-run", action="store_true", help="只体检并打印摘要，不写入")
    restore.add_argument("--no-pre-backup", action="store_true", help="跳过覆盖前的自动预备份（不推荐）")
    restore.add_argument("--include-newer", action="store_true", help="允许恢复 schema 比程序新的备份（排障用）")

    return parser


def main(argv: list[str] | None = None) -> int:
    prefer_console_encoding()
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "log_level", "INFO"))

    command = args.command

    # 这两个命令必须能在库结构异常时照常运行：db/* 用于修复迁移状态，
    # restore/* 用于把库整体换掉。先 init_db 会在坏库上直接抛错，反而挡住救援路径。
    if command not in {"db", "restore"}:
        from app.db import init_db

        init_db()

    if command == "init":
        from app.config import settings

        print(f"数据库已初始化：{settings.db_path}")
        return 0

    if command == "db":
        return _db_command(args)

    if command == "backup":
        return _backup_command(args)

    if command == "restore":
        return _restore_command(args)

    if command == "stats":
        return _stats()

    if command == "doctor":
        return _doctor()

    if command == "dev":
        return _dev(args)

    if command == "data-quality":
        return _data_quality(args)

    if command == "user":
        return _user_command(args)

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
    from app.core.console import detect_console_encoding, explain_console_encoding, locale_encoding

    print("StockMaster 环境自检")
    print(f"  Python       : {platform.python_version()} ({platform.system()} {platform.release()})")
    print(f"  项目根目录   : {REPO_ROOT}")
    print(f"  数据目录     : {settings.data_path}  (存在: {settings.data_path.exists()})")
    print(f"  数据库       : {settings.db_path}  (存在: {settings.db_path.exists()})")
    print(f"  控制台编码   : {detect_console_encoding()}  (进程 locale: {locale_encoding()})")
    if settings.console_encoding_override:
        print(f"  编码覆盖     : SM_CONSOLE_ENCODING={settings.console_encoding_override}")

    # These signals conflict on real machines (chcp=936 while GetConsoleOutputCP=65001
    # on this host), so print them all — otherwise diagnosing mojibake means guessing
    # which layer is lying.
    info = explain_console_encoding()
    print(
        "  编码诊断     : "
        f"chcp/console={info.get('get_console_output_cp')} "
        f"stdout={info.get('stdout_encoding')} "
        f"stderr={info.get('stderr_encoding')} "
        f"isatty={info.get('stdout_isatty')}"
    )
    print(f"                 (输出固定为 {info.get('selected')}；如需覆盖请设置 SM_CONSOLE_ENCODING)")
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


def _data_quality(args) -> int:
    """Print a data quality report, optionally failing when coverage is too low.

    Exit code is non-zero when ``--min-usable-share`` is set and the usable share
    falls below it, so this can gate a pipeline or CI job.
    """
    from app.db import session_scope
    from app.sync.quality import check_data_quality, summarize_for_log

    codes = [c.strip() for c in getattr(args, "codes", "").split(",") if c.strip()]

    with session_scope() as session:
        report = check_data_quality(
            session,
            codes=codes or None,
            adjust=args.adjust,
            stale_days=args.stale_days,
            max_symbols=args.max_symbols,
        )

    coverage = report.coverage or {}

    print("数据质量体检")
    print(f"  检查序列     : {report.adjust}")
    print(f"  全市场最新日 : {report.latest_market_date or '无'}")
    print(f"  交易日历天数 : {report.calendar_sessions or '未同步'}")
    print(
        "  覆盖率       : "
        f"可用 {coverage.get('stocks_usable', 0)} / {coverage.get('stocks_total', 0)} 只"
        f"（{coverage.get('usable_share', 0):.1%}），"
        f"未同步 {coverage.get('stocks_without_series', 0)} 只"
    )
    print(f"  逐只检查     : {report.checked} 只，健康 {report.healthy_symbols}，问题 {report.unhealthy_symbols}")
    print(f"  错误/警告    : {report.error_count} / {report.warning_count}")

    issues = report.issue_counts()
    if issues:
        print("  问题分布     :")
        for kind, count in issues.items():
            print(f"    - {kind}: {count}")

    for finding in report.global_findings:
        print(f"  [全局/{finding.severity}] {finding.message}")

    problems = [s for s in report.symbols if s.findings]
    if problems:
        print("  问题标的（最多 10 条）:")
        for symbol in problems[:10]:
            kinds = ", ".join(f.kind for f in symbol.findings)
            print(f"    - {symbol.code} ({symbol.name}) bars={symbol.bars}: {kinds}")

    print(f"  {summarize_for_log(report)}")

    threshold = getattr(args, "min_usable_share", 0.0) or 0.0
    if threshold > 0 and coverage.get("usable_share", 0.0) < threshold:
        print(
            f"[失败] 可用占比 {coverage.get('usable_share', 0):.1%} "
            f"低于阈值 {threshold:.1%}；请先补齐数据（sync-daily --full）。"
        )
        return 1
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
        retries=args.retries,
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


def _dev(args) -> int:
    """Run backend and frontend together in the current console.

    Signal handling lives here rather than in `run_dev`, because
    `signal.signal()` only works on the main thread — installing it deeper would
    silently fail whenever the orchestrator is hosted, leaving Ctrl+C unhandled.
    """
    import signal
    import threading

    from app.config import settings
    from app.dev_server import run_dev

    host = args.host or settings.host
    port = args.port or settings.port
    frontend_port = args.frontend_port or 5173

    stop_event = threading.Event()
    previous: dict[int, object] = {}

    def request_stop(*_args) -> None:
        # Just flag it: the orchestrator performs an ordered tree shutdown.
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError, AttributeError):
            # Fails off the main thread or on platforms lacking the signal.
            previous[sig] = signal.signal(sig, request_stop)

    try:
        return run_dev(
            host=host,
            port=port,
            frontend_port=frontend_port,
            with_frontend=not args.no_frontend,
            reload=not args.no_reload,
            stop_event=stop_event,
        )
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


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


def _db_command(args) -> int:
    """数据库迁移子命令（不经过 init_db，见 main() 的说明）。"""
    from app.db import migrations

    action = args.db_command

    if action == "current":
        current = migrations.current_revision()
        head = migrations.head_revision()
        print(f"当前版本：{current or '未纳管（库中无 alembic_version）'}")
        print(f"最新版本：{head}")
        if current == head:
            print("状态：已是最新")
            return 0
        print("状态：需要执行 `stockmaster db upgrade`")
        return 1 if current is None else 0

    if action == "history":
        rows = migrations.history()
        if not rows:
            print("迁移链为空")
            return 0
        print(f"{'版本':<24}{'已应用':<8}说明")
        for row in rows:
            print(f"{row['revision']:<24}{'✓' if row['applied'] else '':<8}{row['description']}")
        return 0

    if action == "upgrade":
        before = migrations.current_revision()
        after = migrations.ensure_migrated()
        if before == after:
            print(f"已是最新版本：{after}")
        else:
            print(f"已从 {before or '未纳管'} 升级到 {after}")
        return 0

    if action == "stamp":
        if args.revision != "head":
            print("当前只支持 --revision head", file=sys.stderr)
            return 2
        head = migrations.stamp_head()
        print(f"已写入版本标记：{head}（未执行任何迁移）")
        return 0

    if action == "check":
        drift = migrations.schema_drift()
        if not drift:
            print("schema 与 ORM 模型一致")
            return 0
        print(f"发现 {len(drift)} 处差异（迁移链可能不完整）：")
        for item in drift:
            print(f"  - {item}")
        return 1

    print(f"未知的 db 子命令：{action}", file=sys.stderr)
    return 2


def _backup_command(args) -> int:
    """``stockmaster backup``。"""
    from app.maintenance import BackupError, create_backup, list_backups

    if args.list_only:
        rows = list_backups()
        if not rows:
            print("本地暂无备份")
            return 0
        print(f"{'文件':<58}{'大小':>10}  {'行数':>8}  备注")
        for row in rows:
            size = (
                f"{row['size'] / 1024 / 1024:.2f} MB" if row["size"] >= 1024 * 1024 else f"{row['size'] / 1024:.0f} KB"
            )
            flag = "  [损坏] " + row.get("error", "") if row.get("error") else ""
            print(f"{row['name']:<58}{size:>10}  {row.get('rows', 0):>8}  {row.get('note', '')}{flag}")
        return 0

    try:
        archive = create_backup(
            output=Path(args.output) if args.output else None,
            include_models=not args.no_models,
            include_secret=False if args.no_secret else None,
            note=args.note,
        )
    except BackupError as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return 1

    print(f"备份完成：{archive}")
    print(f"大小：{archive.stat().st_size / 1024 / 1024:.2f} MB")
    if args.no_secret:
        print("提示：未包含签名密钥，恢复后所有已登录会话需要重新登录。")
    return 0


def _restore_command(args) -> int:
    """``stockmaster restore``（main() 已跳过 init_db，坏库也能救）。"""
    from app.maintenance import RestoreError, inspect_backup, restore_backup

    archive = Path(args.archive).expanduser()
    try:
        if args.dry_run:
            inspection = inspect_backup(archive)
            print("[dry-run] 归档有效：")
            print(inspection.summary)
            print(f"归档大小：{inspection.archive_size / 1024 / 1024:.2f} MB")
            return 0

        report = restore_backup(
            archive,
            force=args.force,
            allow_newer=args.include_newer,
            pre_backup=not args.no_pre_backup,
        )
    except RestoreError as exc:
        print(f"恢复失败：{exc}", file=sys.stderr)
        return 1

    print("恢复完成：")
    print(f"  来源版本：{report.app_version or '未知'}（{report.created_at or '未知时间'}）")
    print(f"  数据库：{report.tables} 张表 / {report.rows} 行，schema {report.schema_version}")
    if report.schema_restamped:
        print("  该备份无迁移版本记录，已标记为最新版本")
    print(f"  模型产物：{report.models_restored} 个文件")
    print(f"  签名密钥：{'已恢复' if report.secret_restored else '未恢复'}")
    if report.pre_backup:
        print(f"  预备份：{report.pre_backup}")
    for warning in report.warnings:
        print(f"  注意：{warning}")
    print("建议重启服务，让所有组件状态一致（调度器已自动恢复）。")
    return 0


def _generate_password() -> str:
    """Random password that satisfies the policy (letters + digits, >= 12)."""
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(14))
        if any(c.isalpha() for c in candidate) and any(c.isdigit() for c in candidate):
            return candidate


def _user_command(args) -> int:
    """Account operations for operators without UI access (break-glass path)."""
    from sqlalchemy import select

    from app.auth import service
    from app.db import session_scope
    from app.db.models import User, UserIdentity

    def fail(message: str) -> int:
        print(f"错误：{message}", file=sys.stderr)
        return 1

    with session_scope() as session:
        if args.user_command == "list":
            users = session.execute(select(User).order_by(User.id)).scalars().all()
            if not users:
                print("暂无账号。用 `stockmaster user add --username <名字>` 创建，首个账号将自动成为管理员。")
                return 0
            print(f"{'ID':<5}{'用户名':<20}{'角色':<8}{'状态':<10}{'微信':<6}最近登录")
            for user in users:
                bound = session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id)).scalars().all()
                last = user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "从未"
                print(f"{user.id:<5}{user.username:<20}{user.role:<8}{user.status:<10}{len(bound):<6}{last}")
            return 0

        username = getattr(args, "username", "")

        if args.user_command == "add":
            password = args.password or _generate_password()
            try:
                user = service.create_user(
                    session,
                    username=username,
                    password=password,
                    email=args.email,
                    nickname=args.nickname,
                    role=args.role,
                    status=args.status,
                )
            except (service.AuthError, ValueError) as exc:
                return fail(getattr(exc, "message", str(exc)))
            print(f"已创建账号 {user.username}（角色 {user.role}）")
            if not args.password:
                print(f"初始密码：{password}")
            return 0

        user = service.get_user_by_username(session, username)
        if user is None:
            return fail(f"账号 {username} 不存在")

        if args.user_command == "passwd":
            password = args.password or _generate_password()
            try:
                service.set_password(session, user, password)
            except ValueError as exc:
                return fail(str(exc))
            print(f"已重置 {user.username} 的密码，旧令牌全部失效")
            if not args.password:
                print(f"新密码：{password}")
            return 0

        if args.user_command == "role":
            if args.role is None and args.status is None:
                return fail("请至少指定 --role 或 --status")
            if args.role is not None:
                user.role = args.role
            if args.status is not None:
                user.status = args.status
                if args.status == "disabled":
                    user.token_version += 1
            session.commit()
            print(f"{user.username} 当前：角色 {user.role}，状态 {user.status}")
            return 0

        if args.user_command == "delete":
            if service.count_admins(session, exclude_id=user.id) == 0 and user.role == "admin":
                return fail("至少需要保留一名启用的管理员")
            session.delete(user)
            session.commit()
            print(f"已删除 {username}")
            return 0

        if args.user_command == "unbind-wechat":
            identities = list(session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id)).scalars())
            if not identities:
                print(f"{username} 未绑定微信")
                return 0
            for identity in identities:
                session.delete(identity)
            session.commit()
            print(f"已解绑 {username} 的 {len(identities)} 个微信绑定")
            return 0

    return fail(f"未知的 user 子命令：{args.user_command}")


if __name__ == "__main__":
    raise SystemExit(main())
