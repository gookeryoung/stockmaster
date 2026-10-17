"""管理员运维接口：系统备份与恢复。

放在 ``/admin`` 前缀且整组要求管理员：备份归档里含全站数据，恢复会覆盖线上库，
这两个动作都不该由普通账号触发。恢复与删除写审计（``auth_audit_logs``），回答
"谁在什么时候把库换回了哪一版"。

与 ``deploy/deploy.sh`` 的卷备份互补：那边是整卷回滚，这边是可下载/可上传的
应用级归档（带 manifest、恢复时自动迁移 schema）。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from app.auth import service
from app.auth.deps import client_agent, client_ip, require_admin
from app.config import settings
from app.core.logging import get_logger
from app.db.models import User
from app.maintenance import (
    BackupError,
    RestoreError,
    create_backup,
    inspect_backup,
    list_backups,
    prune_backups,
    restore_backup,
)

log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# 上传上限。注意这里限制的是**磁盘**占用，不是内存：上传一律分块落盘，
# 因为生产机器只有 2 GB 内存，把归档整个读进 bytes 会直接把进程打挂。
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
CHUNK = 1024 * 1024


def _safe_name(name: str) -> str:
    """只接受 backups 目录下的裸文件名，挡掉 ``../`` 之类的路径穿越。"""
    candidate = Path(name).name
    if candidate != name or not candidate.endswith(".tar.gz"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法的备份文件名")
    return candidate


def _local_path(name: str) -> Path:
    path = settings.backups_path / _safe_name(name)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"备份 {name} 不存在")
    return path


async def _save_body(request: Request, dest: Path) -> int:
    """把请求体分块写到磁盘，返回字节数。

    刻意用 **raw body** 而不是 multipart：multipart 需要 ``python-multipart``，
    而本项目的原则是运行期依赖不轻易增加（JWT 也是为此外包的）。raw body 同时
    解决了内存问题 —— ``await request.body()`` 会把 2 GB 归档整个读进内存，
    2C2G 的机器会直接 OOM，这里逐块落盘，内存占用恒定。
    """
    total = 0
    with dest.open("wb") as out:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"备份文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 上限",
                )
            out.write(chunk)
    if total == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体为空，未收到备份文件")
    return total


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #


@router.get("/backups", dependencies=[Depends(require_admin)])
def admin_list_backups(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """本地备份列表（新→旧），含 manifest 摘要与磁盘占用。"""
    items = list_backups(limit=limit)
    return {
        "directory": str(settings.backups_path),
        "keep": settings.backup_keep,
        "total_size": sum(item["size"] for item in items),
        "items": items,
    }


@router.post("/backups", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
def admin_create_backup(
    note: Annotated[str, Query(max_length=200)] = "",
    include_models: bool = True,
    include_secret: bool = True,
) -> dict:
    """在服务器本地创建一份备份（随后可从列表里下载）。"""
    try:
        archive = create_backup(
            include_models=include_models,
            include_secret=include_secret,
            note=note or "manual",
        )
    except BackupError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    prune_backups()
    return {
        "name": archive.name,
        "size": archive.stat().st_size,
        "path": str(archive),
        "warnings": [] if include_secret else ["未包含签名密钥：用这份备份恢复后所有人都要重新登录"],
    }


@router.get("/backups/{name}", dependencies=[Depends(require_admin)])
def admin_download_backup(name: str) -> FileResponse:
    """下载一份本地备份。"""
    path = _local_path(name)
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/backups/{name}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
def admin_delete_backup(name: str) -> None:
    """删除一份本地备份。"""
    _local_path(name).unlink()


# --------------------------------------------------------------------------- #
# 恢复
# --------------------------------------------------------------------------- #


@router.post("/restore/inspect", dependencies=[Depends(require_admin)])
async def admin_restore_inspect(request: Request) -> dict:
    """两步恢复的第一步：只看不写，确认这份归档是什么。

    同时返回当前库的规模，让前端能把「用什么覆盖什么」摆在一屏里再让用户确认。
    """
    temp_root = Path(tempfile.mkdtemp(prefix="sm-inspect-"))
    try:
        target = temp_root / "upload.tar.gz"
        await _save_body(request, target)
        inspection = inspect_backup(target)
        return {
            "manifest": inspection.manifest,
            "summary": inspection.summary,
            "archive_size": inspection.archive_size,
            "known_schema": inspection.known_schema,
            "backup_ahead": inspection.backup_ahead,
            "target": _target_summary(),
        }
    except RestoreError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


@router.post("/restore")
async def admin_restore(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    confirm: bool = False,
    allow_newer: bool = False,
    filename: str = "upload.tar.gz",
) -> dict:
    """两步恢复的第二步：覆盖线上库。

    ``confirm`` 必须为 true —— 这是把「点一下」和「想清楚了」区分开的唯一开关。
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="恢复会覆盖全站数据，请显式确认（confirm=true）",
        )

    temp_root = Path(tempfile.mkdtemp(prefix="sm-restore-"))
    try:
        target = temp_root / "upload.tar.gz"
        await _save_body(request, target)
        report = restore_backup(target, force=True, allow_newer=allow_newer)
    except RestoreError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    _audit_restore(admin, report, filename, client_ip(request), client_agent(request))
    return {
        "status": "ok",
        "message": "恢复完成。建议在空闲时重启服务，让所有组件状态一致。",
        "report": report.to_dict(),
    }


def _audit_restore(admin: User, report, filename: str, ip: str, agent: str) -> None:
    """审计写到恢复后的新库里（旧连接已重置，这里重新开会话）。"""
    from app.db import session_scope

    try:
        with session_scope() as session:
            service.audit(
                session,
                "system.restore",
                username=admin.username,
                detail=f"{filename} | {report.app_version} @ {report.created_at}",
                ip=ip,
                user_agent=agent,
            )
    except Exception as exc:  # pragma: no cover - 审计失败不影响恢复结果
        log.debug("restore audit failed: %s", exc)


def _target_summary() -> dict:
    """当前库的规模摘要（恢复页用来对比「覆盖前 / 覆盖后」）。"""
    import sqlite3

    from app.db import migrations

    path = settings.db_path
    if not path.is_file():
        return {"tables": 0, "rows": 0, "schema_version": ""}
    conn = sqlite3.connect(str(path))
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            if row[0] != "alembic_version"
        ]
        rows = 0
        for table in tables:
            try:
                rows += conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return {"tables": len(tables), "rows": rows, "schema_version": migrations.current_revision() or ""}


__all__ = ["router"]
