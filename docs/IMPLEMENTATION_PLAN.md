# 实施计划：Alembic 迁移 · 用户隔离 · 备份恢复

> 关联文档：`docs/USER_ISOLATION.md`（隔离的设计取舍）
> 本文只回答三件事：**现在做到哪、下一步做什么、每一步怎么验收和回滚**。

---

## 1. 现状（P0 已完成）

| 能力 | 状态 | 落地位置 |
|---|---|---|
| Alembic 迁移链 | ✅ 已接入 | `alembic.ini`、`backend/migrations/`、`app/db/migrations.py` |
| 存量库认领（无版本表的旧库） | ✅ 自动 | `ensure_migrated()` 判 `alembic_version` |
| 数据库版本 CLI | ✅ | `stockmaster db current / history / upgrade / stamp / check` |
| 应用级备份（含 manifest） | ✅ | `app/maintenance/backup.py`、`stockmaster backup` |
| 备份体检（dry-run） | ✅ | `app/maintenance/inspect.py`、`--dry-run`、`POST /admin/restore/inspect` |
| 恢复（含预备份 / 恢复后迁移） | ✅ | `app/maintenance/restore.py`、`stockmaster restore` |
| 备份管理 API + 设置页入口 | ✅ | `api/v1/admin.py`、`components/BackupPanel.tsx` |
| 用户数据隔离 | ❌ 未开始 | 设计与迁移方案见 `USER_ISOLATION.md` |

### 关键设计决定（已落地，改动前先读）

1. **schema 真源仍是 ORM 模型**。迁移链只承载增量，baseline 是空的。
   建库顺序固定为 `create_all → ensure_migrated → create_all`，因此：
   - 全新库：`create_all` 建齐 → 版本表打到 head；
   - 存量库（`create_all` 时代，21 张表但无 `alembic_version`）：**stamp 认领，不重放 baseline**
     （重放会撞 `table already exists`）；
   - 已纳管库：按 `current → head` 应用增量。
2. **`render_as_batch=True`**。SQLite 不支持 `DROP CONSTRAINT` / `ALTER COLUMN`，
   隔离要改的唯一约束必须靠 batch 重建表，没有这个开关迁移会直接语法错误。
3. **备份用 SQLite 在线备份协议**（`sqlite3.Connection.backup`），不是文件拷贝：
   生产库开着 WAL 且后台在写，直接拷 `.db` 会丢 `-wal` 里未 checkpoint 的事务。
4. **恢复是"能回退"的**：体检 → 非空库要 `force`/`confirm` → 自动预备份 → 覆盖 →
   恢复后迁移 → 重置连接池 → 重启调度器。模型目录用暂存 + 原子 rename 替换。
5. **上传走 raw body，不用 multipart**。`python-multipart` 未在依赖里，
   而 multipart 会把整个归档读进内存 —— 2C2G 的机器恢复一个 1 GB 备份就 OOM。
   现在按 1 MB 分块落盘，内存占用恒定。

---

## 2. 阶段划分

```
P0 基础设施（已完成）
 ├─ Alembic 接入 + 存量库认领
 └─ 备份 / 恢复 / 体检 / 审计 / 保留策略
        │
        ▼
P1 用户隔离（下一个迭代，2 个 PR）
 ├─ PR-1 加归属列 + 0002 迁移 + 写入记录（行为不变，可灰度）
 └─ PR-2 读路径隔离 + 预警引擎按 owner 解析 + 测试矩阵
        │
        ▼
P2 备份自动化（1 个 PR）
 ├─ 每日定时备份 + 保留轮转 + 失败告警
 └─ 季度恢复演练（手动 runbook）
        │
        ▼
P3 按用户通知与配额（可选，按需）
```

---

## 3. P1 用户隔离

设计细节（三类数据边界、`owner_id=0` 的语义、为什么管理员不跨用户读）已在
`USER_ISOLATION.md` 里定稿，这里只列**改动清单与顺序**。

### PR-1：加列 + 迁移 + 写路径（行为不变）

| 文件 | 改动 |
|---|---|
| `backend/app/db/models.py` | `WatchlistItem` / `AlertRule` / `AlertTrigger` / `BacktestRun` 加 `owner_id: int = 0`；唯一约束改 `(owner_id, list_name, code)`；新增 4 个复合索引 |
| `backend/migrations/versions/0002_owner_columns.py` | 四张表 `add_column`（`NOT NULL DEFAULT 0`）；`watchlist_items` 用 `batch_alter_table` 重建唯一约束；`alert_triggers.owner_id` 从 `alert_rules` 回填 |
| `backend/app/auth/deps.py` | 新增 `current_owner()`：登录用户取 `user.id`，匿名回落 `GUEST_OWNER_ID = 0` |
| 各 `create_*` | 显式写 `owner_id=owner`（不依赖模型默认值 0） |
| `backend/app/cli.py` | `stockmaster user claim --username <name> [--dry-run]`：把 owner 0 的历史数据交给某个账号 |
| `backend/app/config.py` | `data_isolation: bool = False`（灰度开关，默认关 = 现状） |

**迁移写法（关键片段）**

```python
def upgrade() -> None:
    for table in ("watchlist_items", "alert_rules", "alert_triggers", "backtest_runs"):
        op.add_column(table, sa.Column("owner_id", sa.Integer(), nullable=False, server_default="0"))

    # SQLite 改不了约束：batch 模式会建新表→搬数据→换名，一步到位
    with op.batch_alter_table("watchlist_items", schema=None) as batch:
        batch.drop_constraint("uq_watchlist_item", type_="unique")
        batch.create_unique_constraint("uq_watchlist_item", ["owner_id", "list_name", "code"])
        batch.create_index("ix_watchlist_owner", ["owner_id", "list_name", "code"])

    # 触发记录冗余 owner，避免列表页 join 回 alert_rules
    op.execute(
        "UPDATE alert_triggers SET owner_id = "
        "(SELECT owner_id FROM alert_rules WHERE alert_rules.id = alert_triggers.rule_id) "
        "WHERE owner_id = 0"
    )
```

**验收**

- `stockmaster db upgrade` 在「有数据的旧库」上跑通，行数不变、`uq_watchlist_item` 已含 owner；
- 关掉 `SM_DATA_ISOLATION` 时，全量测试与手工回归与升级前完全一致；
- `db check` 无缺列。

**回滚**：`db downgrade 0001_baseline`（batch 再重建回旧约束）；或直接不启用隔离开关。

### PR-2：读路径隔离

| 文件 | 改动 |
|---|---|
| `backend/app/db/scoped.py`（新增） | `owned_stmt()` / `owned_or_404()`：非本人一律 **404**（403 会泄露 id 是否存在） |
| `api/v1/system.py` | 自选股 4 个端点按 owner 过滤 |
| `api/v1/alerts.py` | 规则/触发/扫描/预览按 owner；`/alerts/scan` 只扫本人 |
| `api/v1/backtest.py` | 历史与详情按 owner |
| `alert/engine.py` | `_resolve_scope` 的 `watchlist` 分支按 `(rule.owner_id, list_name)` 取；**这是最容易漏的一行** |
| `jobs/tasks.py` | `_default_universe()` / `job_snapshot_poll` 取所有用户自选的**并集**（共享行情数据只存一份） |
| `frontend` | 基本无改动；用户页可加只读统计列（自选数/规则数/回测数） |

**验收（= 测试矩阵，`backend/tests/test_isolation.py`）**

两个账号 A/B 对每个私有端点交叉访问，断言 404 或空列表：

```python
PRIVATE_CASES = [
    ("GET",    "/api/v1/watchlist"),
    ("DELETE", "/api/v1/watchlist/{a_item}"),
    ("GET",    "/api/v1/alerts/rules"),
    ("PATCH",  "/api/v1/alerts/rules/{a_rule}"),
    ("GET",    "/api/v1/alerts/triggers"),
    ("GET",    "/api/v1/backtest/{a_run}"),
]
```

另外必须覆盖：两人同名 `default` 清单各加同一只股票都成功（旧约束会串改 `note`）；
B 的 `scope=watchlist` 规则只命中 B 的自选；匿名（`SM_AUTH_ENABLED=false`）落 owner 0 可读写。

**回滚**：`SM_DATA_ISOLATION=false` 立即回到共享语义（列还在，无害）。

---

## 4. P2 备份自动化

| 项 | 做法 |
|---|---|
| 定时备份 | 调度器加 `backup` job：交易日 17:30（预警扫描之后），调 `create_backup(note="auto")` |
| 保留轮转 | 复用 `prune_backups()` + `SM_BACKUP_KEEP`（默认 7） |
| 失败告警 | 备份失败走现有 `notify()` 通道；失败不影响调度器后续任务 |
| 恢复演练 | 每季度手动：下载最新备份 → 在临时目录 `SM_DATA_DIR=/tmp/drill` 恢复 → `db check` → 删掉 |
| 异地留存（可选） | 归档同步到对象存储（COS），本地只留 3 份；需新增 `cos-python-sdk-v5` 依赖，暂不做 |

验收：连续 7 天自动备份存在且 `backup --list` 可读 manifest；磁盘占用稳定在 `keep × 单份大小`。

---

## 5. 上线 Runbook（本次改动）

```bash
# 1) 拉代码（本次新增了 alembic.ini、backend/migrations、app/maintenance，必须重建镜像）
cd /opt/stockmaster && git pull

# 2) 部署前先看清当前库版本与数据量
docker compose --env-file .env -f docker-compose.prod.yml exec app \
  python -m app.cli db current

# 3) 构建并发布（deploy.sh 会自动备份 /data 卷，保留 5 份）
./deploy/deploy.sh

# 4) 确认启动时已自动认领版本
docker compose --env-file .env -f docker-compose.prod.yml exec app \
  python -m app.cli db current      # 应输出 当前=最新=0001_baseline

# 5) 手工打一份基线备份（之后的任何操作都有退路）
docker compose --env-file .env -f docker-compose.prod.yml exec app \
  python -m app.cli backup --note "alembic-introduced"
```

演练恢复（**先在本地/临时数据目录做**）：

```bash
SM_DATA_DIR=/tmp/drill python -m app.cli restore ~/backups/xxx.tar.gz --dry-run
SM_DATA_DIR=/tmp/drill python -m app.cli restore ~/backups/xxx.tar.gz --force
```

### 需要的配置项（都有默认值，可不动）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SM_DB_AUTO_MIGRATE` | `true` | 启动自动迁移；设 false 则要求先 `db upgrade`（版本落后会告警） |
| `SM_BACKUP_KEEP` | `7` | 本地备份保留份数，`0` 不清理 |
| `SM_BACKUP_INCLUDE_SECRET` | `true` | 归档是否含 `.auth_secret` |

> `docker-compose.prod.yml` 逐项列出 environment，新增变量**必须**同步进去，否则传不进容器。
> 这三项走 `.env` 默认值即可，暂不加进 compose；需要改时再补（P2 一并处理）。

---

## 6. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 镜像漏拷 `alembic.ini` / `migrations` | 容器内 `db` 命令报「迁移目录缺失」 | Dockerfile.prod 已加 `COPY alembic.ini ./`；`backend/migrations` 随 `COPY backend/` 进入 |
| 存量库被误 `stamp` 到 head 但结构落后 | 少了列，业务查询才报错 | `ensure_migrated` 之后跑 `schema_drift()` 告警；`db check` 可显式体检 |
| 恢复期间仍有 HTTP 写请求 | 数据竞争 | 恢复前暂停调度器 + 进程内互斥锁；**完成后建议重启服务**（响应里已提示） |
| 备份含签名密钥 | 归档泄露 = 可伪造会话 | manifest 标注 `secrets.auth_secret`，检查页与列表都提示；可 `--no-secret` / `include_secret=false` |
| 全市场库备份体积大 | 50 GB 磁盘被备份吃满 | `SM_BACKUP_KEEP` 限制份数；deploy.sh 的卷备份另有 5 份上限；必要时降 `keep` |
| 同一秒两次备份 | 后一份覆盖前一份 | 已修：`_unique_path()` 自动追加 `-2`、`-3` |
| SQLite 在 Windows 上文件被占用 | 迁移/清库失败 | 备份恢复一律走连接协议而非删文件；测试里显式 `reset_engine()` |

---

## 7. 未决项

1. 隔离是否需要「共享清单」（团队公用一份 `default`）：默认按纯私有实施，`owner_id=0` 可复用为全员可见。
2. 回测结果是否支持发链接共享：需要 `visibility` + `share_token`，而不是放宽 owner 过滤。
3. 备份是否异地留存（COS）：等本地轮转稳定运行一段时间再评估。
