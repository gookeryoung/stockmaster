"""从备份归档恢复数据（破坏性操作）。

安全设计
--------
恢复会整体替换线上库，所以每一步都按「能回退」来写：

1. **先体检**：``inspect_backup`` 校验归档完整性与协议版本，备份比程序新则拒绝；
2. **目标非空要显式确认**：库里有数据时必须 ``force=True``，否则直接拒绝；
3. **自动预备份**：真正覆盖前先打一份 ``note="pre-restore"`` 的归档，误操作可退回；
4. **在线替换**：用 ``sqlite3.Connection.backup`` 覆盖目标库，而不是删文件再拷贝 ——
   Windows/容器里目标文件常被连接池持有，删不掉；这种覆盖还能让旧连接在下一个
   事务看到新数据；
5. **恢复后迁移**：备份来自旧版程序时自动 ``upgrade head``；备份没有版本记录
   （create_all 时代）则 ``stamp head`` 认领，再用 ``schema_drift`` 报告差异；
6. **重置连接池 + 暂停调度器**：避免旧连接读到旧页缓存、避免定时任务在恢复途中写入。

模型目录用「暂存目录 → 原子 rename」替换，中途失败旧目录保持不动。
"""

from __future__ import annotations

import shutil
import sqlite3
import tarfile
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.core.logging import get_logger
from app.maintenance.backup import ARCHIVE_ROOT, BackupError, create_backup
from app.maintenance.inspect import RestoreError, inspect_backup

log = get_logger(__name__)

# 同一进程内只允许一个恢复任务：两个并发恢复会互相覆盖，结果不可预测。
_RESTORE_LOCK = threading.Lock()


@dataclass
class RestoreReport:
    """恢复结果摘要（给 CLI / API 回显）。"""

    app_version: str = ""
    created_at: str = ""
    tables: int = 0
    rows: int = 0
    schema_version: str = ""
    schema_restamped: bool = False
    models_restored: int = 0
    secret_restored: bool = False
    pre_backup: str = ""
    scheduler_paused: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "app_version": self.app_version,
            "created_at": self.created_at,
            "tables": self.tables,
            "rows": self.rows,
            "schema_version": self.schema_version,
            "schema_restamped": self.schema_restamped,
            "models_restored": self.models_restored,
            "secret_restored": self.secret_restored,
            "pre_backup": self.pre_backup,
            "scheduler_paused": self.scheduler_paused,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# 前置检查
# --------------------------------------------------------------------------- #


def _check_target_safe(force: bool) -> None:
    """目标库已有数据时要求显式 ``force``。

    用 ``SELECT 1 ... LIMIT 1`` 逐表探测而不是 COUNT(*)：日线表可能有上千万行，
    COUNT 在恢复前的检查里没有意义，只要知道「非空」就够了。
    """
    db_path = settings.db_path
    if not db_path.is_file():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            # alembic_version 是迁移基础设施，不算"用户数据"，别让它吓到人
            if row[0] != "alembic_version"
        ]
        nonempty = []
        for table in tables:
            # 表名来自 sqlite_master 枚举，不是外部输入
            if conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                nonempty.append(table)
    except sqlite3.Error as exc:
        raise RestoreError(f"无法检查目标数据库: {exc}") from exc
    finally:
        conn.close()

    if nonempty and not force:
        preview = "、".join(nonempty[:5]) + ("…" if len(nonempty) > 5 else "")
        raise RestoreError(
            f"目标库已有数据（{len(nonempty)} 张非空表：{preview}）。恢复会整体覆盖，"
            f"确认请加 --force（网页端需再次确认）。"
        )


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #


def _restore_sqlite(extracted: Path, db_rel: str, target: Path) -> None:
    """用 SQLite 在线备份协议把归档库覆盖到目标路径。"""
    src_db = extracted / ARCHIVE_ROOT / db_rel
    if not src_db.is_file():
        raise RestoreError(f"归档内缺少数据库文件: {src_db}")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(src_db))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise RestoreError(f"写入目标数据库失败: {exc}") from exc
    finally:
        src.close()
    log.info("数据库已恢复 → %s", target)


def _restore_tree(src: Path, target: Path) -> int:
    """用「暂存 → 原子 rename」替换目录，失败时旧目录保持原样。

    直接 ``rmtree(target)`` 再拷贝是最常见的写法，也是最危险的：拷贝到一半失败，
    旧数据就永久没了。这里先完整复制到同级 ``.restore`` 目录（同一文件系统，
    rename 是原子的），再换名。
    """
    if not src.is_dir():
        return 0
    staging = target.parent / f"{target.name}.restore"
    previous = target.parent / f"{target.name}.previous"
    for leftover in (staging, previous):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)

    count = sum(1 for item in src.rglob("*") if item.is_file())
    try:
        shutil.copytree(src, staging)
        if target.exists():
            target.rename(previous)
        staging.rename(target)
    except OSError as exc:
        # 换名失败：把旧目录换回去，别留下半个新目录
        if previous.exists() and not target.exists():
            try:
                previous.rename(target)
            except OSError:  # pragma: no cover - 尽力回滚
                log.warning("恢复目录回滚失败，旧数据保留在 %s", previous)
        shutil.rmtree(staging, ignore_errors=True)
        raise RestoreError(f"替换目录 {target} 失败: {exc}") from exc
    shutil.rmtree(previous, ignore_errors=True)
    return count


def _migrate_after_restore(manifest_schema: str, report: RestoreReport) -> None:
    """把恢复出来的库带到当前程序的 schema 版本。"""
    from app.db import migrations

    known = {str(row["revision"]) for row in migrations.history()}
    if manifest_schema and manifest_schema not in known:
        raise RestoreError(
            f"备份的 schema 版本 {manifest_schema} 不在当前程序的迁移链上，说明备份比程序新。请先升级程序再恢复。"
        )

    if not manifest_schema:
        # create_all 时代的库没有 alembic_version：直接认领，不重放 baseline
        migrations.stamp_head()
        report.schema_restamped = True
        log.info("备份无迁移版本记录，已标记为 head")
    else:
        try:
            migrations.upgrade_to_head()
        except Exception as exc:
            raise RestoreError(f"恢复后迁移失败: {exc}") from exc

    # 迁移链可能有缺口：补建缺失的表（已存在的表不受影响）
    from app.db.base import Base
    from app.db.session import get_engine

    Base.metadata.create_all(get_engine())
    report.schema_version = migrations.current_revision() or ""
    drift = migrations.schema_drift()
    if drift:
        report.warnings.append(
            f"schema 与模型存在 {len(drift)} 处差异（可能来自更早的备份）："
            + "、".join(drift[:5])
            + ("…" if len(drift) > 5 else "")
        )


def _reset_runtime() -> None:
    """恢复后重置连接池：旧连接可能持有恢复前的页缓存与预编译语句。"""
    try:
        from app.db.session import reset_engine

        reset_engine()
    except Exception as exc:  # pragma: no cover - 防御
        log.debug("重置连接池失败（忽略）: %s", exc)


def _pause_scheduler() -> bool:
    """恢复期间停掉定时任务；返回是否真的停过。"""
    try:
        from app.jobs import scheduler

        if scheduler.scheduler_status().get("running"):
            scheduler.stop_scheduler()
            return True
    except Exception as exc:  # pragma: no cover - 防御
        log.debug("暂停调度器失败（忽略）: %s", exc)
    return False


def _resume_scheduler(paused: bool) -> None:
    if not paused:
        return
    try:
        from app.jobs import scheduler

        scheduler.start_scheduler()
    except Exception as exc:  # pragma: no cover - 防御
        log.warning("恢复后重启调度器失败: %s", exc)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def restore_backup(
    archive_path: Path,
    *,
    force: bool = False,
    allow_newer: bool = False,
    include_secret: bool | None = None,
    pre_backup: bool = True,
) -> RestoreReport:
    """从归档恢复数据库与模型产物。

    Args:
        archive_path: ``.tar.gz`` 应用备份。
        force: 目标库非空时必须显式开启。
        allow_newer: 允许恢复「schema 比本程序新」的备份（不安全，仅供排障）。
        include_secret: 是否恢复 ``.auth_secret``；``None`` 跟随配置。
        pre_backup: 覆盖前自动打一份预备份（强烈建议保持默认）。

    Raises:
        RestoreError: 归档非法、版本不兼容、目标不安全、写入/迁移失败。
    """
    if not _RESTORE_LOCK.acquire(blocking=False):
        raise RestoreError("已有恢复任务正在执行，请等待完成后再试")

    report = RestoreReport()
    paused = False
    temp_root: Path | None = None
    try:
        inspection = inspect_backup(archive_path)
        manifest = inspection.manifest
        if inspection.backup_ahead and not allow_newer:
            raise RestoreError("备份的 schema 比当前程序新，无法安全恢复：请先升级程序（或仅在排障时允许不兼容恢复）")

        report.app_version = str(manifest.get("app_version") or "")
        report.created_at = str(manifest.get("created_at") or "")
        db_rel = str((manifest.get("database") or {}).get("path") or "data/stockmaster.db")
        db_info = manifest.get("database") or {}
        report.schema_version = str(db_info.get("schema_version") or "")
        report.tables = len(db_info.get("tables") or [])
        report.rows = sum((db_info.get("row_counts") or {}).values())

        _check_target_safe(force)

        if pre_backup:
            try:
                report.pre_backup = str(create_backup(note="pre-restore", prune=False))
            except BackupError as exc:
                raise RestoreError(f"预备份失败，已取消恢复（避免无退路）: {exc}") from exc

        temp_root = Path(tempfile.mkdtemp(prefix="sm-restore-"))
        with tarfile.open(archive_path, "r:gz") as tar:
            # filter="data"：丢弃设备文件/绝对路径等危险元数据（Python 3.12+）
            tar.extractall(temp_root, filter="data")
        extracted = temp_root

        paused = _pause_scheduler()

        _restore_sqlite(extracted, db_rel, settings.db_path)
        # 先丢连接池：后面的迁移与 schema 自检必须读到恢复后的库，而不是旧页缓存
        _reset_runtime()
        _migrate_after_restore(report.schema_version, report)

        models_src = extracted / ARCHIVE_ROOT / "data" / "models"
        if models_src.is_dir():
            report.models_restored = _restore_tree(models_src, settings.models_dir)

        want_secret = settings.backup_include_secret if include_secret is None else include_secret
        secret_src = extracted / ARCHIVE_ROOT / "data" / settings.auth_secret_path.name
        if want_secret and secret_src.is_file():
            shutil.copy2(secret_src, settings.auth_secret_path)
            report.secret_restored = True
        elif not want_secret:
            report.warnings.append("未恢复签名密钥：已登录会话保持有效，但恢复后新登录会用当前密钥签发")

        report.scheduler_paused = paused
        # 恢复后停过的调度器要重新起来（仅在成功路径，失败时保持停止更安全）
        _resume_scheduler(paused)
        paused = False

        log.info(
            "恢复完成：%s（%d 张表 / %d 行，模型 %d 个文件）",
            report.app_version or "未知版本",
            report.tables,
            report.rows,
            report.models_restored,
        )
        return report
    finally:
        _resume_scheduler(paused)
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
        _RESTORE_LOCK.release()


def restore_from_upload(content: bytes, filename: str = "upload.tar.gz", **kwargs) -> RestoreReport:
    """把上传的字节流落到临时文件再恢复（API 用）。"""
    temp_root = Path(tempfile.mkdtemp(prefix="sm-upload-"))
    try:
        path = temp_root / (Path(filename).name or "upload.tar.gz")
        path.write_bytes(content)
        return restore_backup(path, **kwargs)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


__all__ = ["RestoreError", "RestoreReport", "restore_backup", "restore_from_upload"]
