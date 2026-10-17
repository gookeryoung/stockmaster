"""创建应用级备份归档。

归档结构（manifest 协议 v1）::

    stockmaster-backup-<应用版本>-v1-<UTC时间戳>.tar.gz
    └── backup/
        ├── manifest.json          # 版本、时间、表清单与行数、schema 版本、文件统计
        └── data/
            ├── stockmaster.db     # SQLite 热备份（不是文件拷贝，见下）
            ├── models/*.joblib    # 训练产物
            └── .auth_secret       # 令牌签名密钥（可关）

为什么是「热备份」而不是 ``shutil.copy``
----------------------------------------
生产库开着 WAL 且后台可能在写（每日同步、预警扫描）。直接拷 ``.db`` 会漏掉
``-wal`` 里未 checkpoint 的事务，得到的库可能缺最近的数据甚至不一致。
``sqlite3.Connection.backup`` 走 SQLite 的页级在线备份协议：会在源库被写入时
自动重启备份轮次，最终得到一个事务一致的快照，且不需要停服。
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from stockmaster import __version__
from stockmaster.config import settings
from stockmaster.core.logging import get_logger

log = get_logger(__name__)

MANIFEST_VERSION = "1"
ARCHIVE_ROOT = "backup"


class BackupError(RuntimeError):
    """备份无法完成（库缺失、磁盘不可写、打包失败等）。"""


@dataclass
class DatabaseInfo:
    """数据库快照的统计信息。"""

    path: str = ""
    db_type: str = "sqlite"
    backup_mode: str = "native"
    schema_version: str = ""
    tables: list[str] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class FilesInfo:
    """附属文件（模型产物 / 签名密钥）的统计信息。"""

    included: bool = False
    file_count: int = 0
    total_size: int = 0


@dataclass
class SecretsInfo:
    """是否含令牌签名密钥 —— 含则归档等同凭据，恢复端要提示用户。"""

    auth_secret: bool = False
    wechat_credentials: bool = False


@dataclass
class BackupManifest:
    """``manifest.json`` 的内容。字段只增不改，保号段给未来用。"""

    version: str = MANIFEST_VERSION
    app_version: str = ""
    created_at: str = ""
    note: str = ""
    database: DatabaseInfo = field(default_factory=DatabaseInfo)
    models: FilesInfo = field(default_factory=FilesInfo)
    secrets: SecretsInfo = field(default_factory=SecretsInfo)

    def to_dict(self) -> dict:
        return asdict(self)


def format_backup_filename(prefix: str = "stockmaster-backup", ext: str = ".tar.gz") -> str:
    """``<prefix>-<应用版本>-v<协议版本>-<UTC 时间戳>``，与 CNDB 的命名习惯一致。"""
    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{__version__}-v{MANIFEST_VERSION}-{ts}{ext}"


def _unique_path(path: Path) -> Path:
    """同名时追加序号，绝不覆盖已有归档。

    文件名只精确到秒（``...-20261017T034651Z.tar.gz``），而「恢复前自动预备份」
    很可能紧随一次手动备份发生 —— 直接写会静默覆盖掉刚做好的那一份。
    """
    if not path.exists():
        return path
    stem, suffix = path.name[: -len(".tar.gz")], ".tar.gz"
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise BackupError(f"同一秒内备份过多，无法生成唯一文件名: {path.name}")


def _snapshot_sqlite(db_path: Path, dest: Path) -> tuple[list[str], dict[str, int], str]:
    """把库在线备份到 ``dest``，并从**快照**读取表清单、行数与 schema 版本。

    统计走快照而不是线上库：既避免长时间持有读锁，也保证 manifest 描述的就是
    归档里那份数据。
    """
    if not db_path.is_file():
        raise BackupError(f"数据库文件不存在: {db_path}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite 热备份失败: {exc}") from exc
    finally:
        src.close()

    conn = sqlite3.connect(str(dest))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts: dict[str, int] = {}
        for table in tables:
            # 表名来自 sqlite_master 枚举，不是外部输入
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            schema_version = str(row[0]) if row else ""
        except sqlite3.OperationalError:
            schema_version = ""
    finally:
        conn.close()
    return tables, counts, schema_version


def _copy_dir_files(src: Path, dest: Path) -> FilesInfo:
    """复制目录下的普通文件（跳过隐藏文件与子目录中的临时产物）。"""
    info = FilesInfo()
    if not src.is_dir():
        return info
    for item in sorted(src.rglob("*")):
        if not item.is_file() or item.name.startswith("."):
            continue
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        info.file_count += 1
        info.total_size += item.stat().st_size
    info.included = info.file_count > 0
    return info


def create_backup(
    output: Path | None = None,
    *,
    include_models: bool = True,
    include_secret: bool | None = None,
    note: str = "",
    prune: bool = True,
    db_path: Path | None = None,
    models_dir: Path | None = None,
) -> Path:
    """创建一份完整备份并返回归档路径。

    Args:
        output: 目标 ``.tar.gz``。缺省落到 ``<数据目录>/backups/`` 并按协议命名。
        include_models: 是否带上 ``models/*.joblib``（没有产物时自动跳过）。
        include_secret: 是否带 ``.auth_secret``；``None`` 时跟随配置。
        note: 写进 manifest 的备注（例如触发来源 "pre-restore"）。
        prune: 是否按 ``SM_BACKUP_KEEP`` 清理旧归档。
        db_path / models_dir: 供测试覆盖路径。

    Raises:
        BackupError: 数据库缺失、目录不可写、打包失败。
    """
    settings.ensure_dirs()
    db = db_path or settings.db_path
    models = models_dir or settings.models_dir
    want_secret = settings.backup_include_secret if include_secret is None else include_secret

    if output is None:
        output = settings.backups_path / format_backup_filename()
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output = _unique_path(output)

    temp_root = Path(tempfile.mkdtemp(prefix="sm-backup-"))
    try:
        data_dir = temp_root / ARCHIVE_ROOT / "data"
        data_dir.mkdir(parents=True)

        tables, counts, schema_version = _snapshot_sqlite(db, data_dir / db.name)
        manifest = BackupManifest(
            app_version=__version__,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            note=note,
            database=DatabaseInfo(
                path=f"data/{db.name}",
                schema_version=schema_version,
                tables=tables,
                row_counts=counts,
            ),
        )

        if include_models:
            manifest.models = _copy_dir_files(models, data_dir / "models")

        secret_src = settings.auth_secret_path
        if want_secret and secret_src.is_file():
            shutil.copy2(secret_src, data_dir / secret_src.name)
            manifest.secrets.auth_secret = True

        manifest_path = temp_root / ARCHIVE_ROOT / "manifest.json"
        manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        # GNU_FORMAT：Windows 下路径可能超过 ustar 的 100 字节限制
        with tarfile.open(output, "w:gz", format=tarfile.GNU_FORMAT) as tar:
            tar.add(temp_root / ARCHIVE_ROOT, arcname=ARCHIVE_ROOT)
    except BackupError:
        raise
    except (OSError, sqlite3.Error, tarfile.TarError) as exc:
        raise BackupError(f"备份失败：{type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    total_rows = sum(counts.values())
    log.info(
        "备份完成 %s（%d 张表 / %d 行，模型 %d 个文件，%.2f MB）",
        output.name,
        len(tables),
        total_rows,
        manifest.models.file_count,
        output.stat().st_size / 1024 / 1024,
    )
    if prune:
        prune_backups()
    return output


def list_backups(limit: int = 20) -> list[dict]:
    """列出本地备份（新→旧），尽力读出 manifest 摘要。

    归档损坏时不让整个列表失败：``error`` 字段说明原因，条目仍可删。
    """
    folder = settings.backups_path
    if not folder.is_dir():
        return []
    entries: list[dict] = []
    for path in sorted(folder.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        entry: dict = {
            "name": path.name,
            "size": path.stat().st_size,
            "created_at": dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC).isoformat(),
        }
        try:
            with tarfile.open(path, "r:gz") as tar:
                member = tar.extractfile(f"{ARCHIVE_ROOT}/manifest.json")
                if member is None:
                    raise BackupError("缺少 manifest.json")
                payload = json.loads(member.read().decode("utf-8"))
            entry.update(
                {
                    "app_version": payload.get("app_version", ""),
                    "note": payload.get("note", ""),
                    "schema_version": (payload.get("database") or {}).get("schema_version", ""),
                    "tables": len((payload.get("database") or {}).get("tables") or []),
                    "rows": sum(((payload.get("database") or {}).get("row_counts") or {}).values()),
                    "has_models": bool((payload.get("models") or {}).get("included")),
                    "has_secret": bool((payload.get("secrets") or {}).get("auth_secret")),
                }
            )
        except (OSError, tarfile.TarError, json.JSONDecodeError, BackupError) as exc:
            entry["error"] = str(exc)
        entries.append(entry)
    return entries


def prune_backups(keep: int | None = None) -> list[str]:
    """只保留最近 ``keep`` 份本地备份，返回被删掉的文件名。"""
    limit = settings.backup_keep if keep is None else keep
    folder = settings.backups_path
    if limit <= 0 or not folder.is_dir():
        return []
    archives = sorted(folder.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for path in archives[limit:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:  # pragma: no cover - 权限/占用
            log.warning("删除旧备份失败 %s: %s", path.name, exc)
    if removed:
        log.info("已清理 %d 份旧备份（保留 %d 份）", len(removed), limit)
    return removed
