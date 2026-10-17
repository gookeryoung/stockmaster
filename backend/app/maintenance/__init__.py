"""系统维护：备份、恢复与数据库迁移的对外入口。

与 ``deploy/deploy.sh`` 里的 ``backups/pre-deploy-*.tar.gz`` 分工不同：

* 部署脚本备份的是整个 ``/data`` 卷的原始 tar，用于**回滚整个卷**（不解析内容）；
* 这里产出的是**应用级归档**：带 ``manifest.json``，可以在网页上直接下载/上传，
  恢复时会校验版本、自动跑迁移、重置连接池，因此必须由应用自己读写。

两者可以共存：卷备份兜底灾难恢复，应用归档用于日常「改坏了，回到今早」。
"""

from app.maintenance.backup import (
    BackupError,
    BackupManifest,
    create_backup,
    format_backup_filename,
    list_backups,
    prune_backups,
)
from app.maintenance.inspect import BackupInspection, inspect_backup
from app.maintenance.restore import RestoreError, RestoreReport, restore_backup

__all__ = [
    "BackupError",
    "BackupInspection",
    "BackupManifest",
    "RestoreError",
    "RestoreReport",
    "create_backup",
    "format_backup_filename",
    "inspect_backup",
    "list_backups",
    "prune_backups",
    "restore_backup",
]
