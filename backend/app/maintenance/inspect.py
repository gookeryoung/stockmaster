"""备份体检（dry-run）：恢复前先确认这个归档能不能用。

不修改任何文件系统状态，也不碰当前的库 —— 网页「先检查、再确认恢复」两步流程
的第一步就是它，避免用户上传一个 3 GB 的错文件直接把线上库覆盖掉。
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from app.maintenance.backup import ARCHIVE_ROOT, MANIFEST_VERSION

SUPPORTED_MANIFEST_VERSIONS = {MANIFEST_VERSION}


class RestoreError(RuntimeError):
    """恢复无法完成（归档损坏、版本不兼容、目标不安全）。"""


@dataclass
class BackupInspection:
    """归档体检结果。"""

    manifest: dict
    archive_size: int
    # 备份里的迁移版本是否存在于本程序的迁移链上
    known_schema: bool
    # 备份比程序新（本程序无法理解其 schema），native 恢复会被拒绝
    backup_ahead: bool
    summary: str


def _known_revisions() -> set[str]:
    from app.db import migrations

    return {str(row["revision"]) for row in migrations.history()}


def inspect_backup(archive_path: Path) -> BackupInspection:
    """校验归档可读、manifest 合法，并返回元信息。

    Raises:
        RestoreError: 文件缺失、不是合法 tar.gz、缺 manifest、协议版本不支持。
    """
    path = Path(archive_path)
    if not path.is_file():
        raise RestoreError(f"备份文件不存在: {path}")

    try:
        with tarfile.open(path, "r:gz") as tar:
            names = set(tar.getnames())
            manifest_name = f"{ARCHIVE_ROOT}/manifest.json"
            if manifest_name not in names:
                raise RestoreError("归档缺少 manifest.json，不是 StockMaster 的备份归档")
            member = tar.extractfile(manifest_name)
            if member is None:  # pragma: no cover - tar 结构异常
                raise RestoreError("manifest.json 无法读取")
            manifest = json.loads(member.read().decode("utf-8"))
            # 完整性：manifest 声明的库文件必须真的在归档里
            db_rel = (manifest.get("database") or {}).get("path") or ""
            if db_rel and f"{ARCHIVE_ROOT}/{db_rel}" not in names:
                raise RestoreError(f"归档缺少数据库文件: {ARCHIVE_ROOT}/{db_rel}")
    except RestoreError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        # EOFError：下载被截断时 gzip 流提前结束
        raise RestoreError(f"归档损坏或无法打开: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RestoreError(f"manifest.json 不是合法 JSON: {exc}") from exc

    version = str(manifest.get("version", "?"))
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_MANIFEST_VERSIONS))
        raise RestoreError(f"不支持的备份协议版本 {version}（本程序支持 {supported}），请升级程序后再恢复")

    schema_version = str((manifest.get("database") or {}).get("schema_version") or "")
    known = schema_version in _known_revisions() if schema_version else True
    backup_ahead = bool(schema_version) and not known

    db = manifest.get("database") or {}
    rows = sum((db.get("row_counts") or {}).values())
    models = manifest.get("models") or {}
    secret = manifest.get("secrets") or {}
    lines = [
        f"应用版本 {manifest.get('app_version') or '?'}，创建于 {manifest.get('created_at') or '?'}",
        f"数据库：{len(db.get('tables') or [])} 张表 / {rows} 行，schema {schema_version or '未纳管'}",
        f"模型产物：{models.get('file_count', 0)} 个文件" if models.get("included") else "模型产物：未包含",
        "含令牌签名密钥（归档等同凭据，请妥善保管）" if secret.get("auth_secret") else "不含签名密钥",
    ]
    if backup_ahead:
        lines.append("⚠ 备份的 schema 比当前程序新，无法直接恢复：请先升级程序")

    return BackupInspection(
        manifest=manifest,
        archive_size=path.stat().st_size,
        known_schema=known,
        backup_ahead=backup_ahead,
        summary="\n".join(lines),
    )


__all__ = ["SUPPORTED_MANIFEST_VERSIONS", "BackupInspection", "RestoreError", "inspect_backup"]
