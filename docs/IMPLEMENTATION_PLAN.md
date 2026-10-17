# 实施计划：Alembic 迁移 · 工作区隔离 · 备份恢复

> 关联文档：`docs/WORKSPACE.md`（工作区隔离现行设计）、`docs/USER_ISOLATION.md`（历史设计取舍）
> 本文只回答三件事：**现在做到哪、下一步做什么、每一步怎么验收和回滚**。

---

## 1. 现状（P0 已完成）

| 能力 | 状态 | 落地位置 |
|---|---|---|
| Alembic 迁移链 | ✅ 已接入 | `alembic.ini`、`src/stockmaster/alembic/`、`stockmaster/db/migrations.py` |
| 存量库认领（无版本表的旧库） | ✅ 自动 | `ensure_migrated()` 判 `alembic_version` |
| 数据库版本 CLI | ✅ | `stockmaster db current / history / upgrade / stamp / check` |
| 应用级备份（含 manifest） | ✅ | `stockmaster/maintenance/backup.py`、`stockmaster backup` |
| 备份体检（dry-run） | ✅ | `stockmaster/maintenance/inspect.py`、`--dry-run`、`POST /admin/restore/inspect` |
| 恢复（含预备份 / 恢复后迁移） | ✅ | `stockmaster/maintenance/restore.py`、`stockmaster restore` |
| 备份管理 API + 设置页入口 | ✅ | `api/v1/admin.py`、`components/BackupPanel.tsx` |
| 用户隔离 PR-1 / PR-2（owner_id，已被 0003 取代） | ✅ 已完成 | `0002_owner_columns`、`tests/test_isolation.py` |
| 工作区隔离（Workspace + owner/editor/viewer） | ✅ 已完成，默认关闭 | `0003_workspaces`、`src/stockmaster/plugins/workspaces/`、`tests/test_workspace.py`、见 `WORKSPACE.md` |
| 工作区级通知通道（webhook / Bark） | ✅ 已完成 | `0005_workspace_channels`、`plugins/workspaces/routers/workspaces.py`、`plugins/alerts/services/channels.py`、见 `WORKSPACE.md` §5.1 |
| 工作区配额 / 管理员统计列 | ❌ 未开始 | `WORKSPACE.md` §11 |

### 关键设计决定（已落地，改动前先读）

1. **schema 真源仍是 ORM 模型**。迁移链只承载增量，baseline 是空的。
   建库顺序固定为 `has_application_schema → create_all → ensure_migrated → create_all`，因此：
   - 全新库：`create_all` 建齐 → 直接 stamp head（迁移链无需重放）；
   - 存量库（`create_all` 时代，有表但无 `alembic_version`）：**stamp 到 baseline 再
     `upgrade head`** —— 只 stamp head 会让增量的加列被整条跳过，库自称最新却缺列；
     因此每个迁移脚本都必须写成幂等的（先探测再执行）；
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
P1 数据隔离（已完成）
 ├─ PR-1 加归属列 + 0002 迁移 + 写入记录（行为不变，可灰度）
 ├─ PR-2 读路径隔离 + 预警引擎按归属解析 + 测试矩阵
 ├─ PR-3 升级为工作区隔离：0003 迁移 + owner/editor/viewer 角色 + 成员管理
 ├─ PR-4 工作区级通知通道：0005 迁移 + owner 管理通道 + 按工作区投递
 └─ 待办：管理员统计列、配额
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

### PR-1：加列 + 迁移 + 写路径（行为不变）—— ✅ 已完成

> 落地后的差异：迁移是幂等的（先探测列/约束是否存在），因为同一条迁移既要在
> 「列已存在」的全新库上跑，也要在「列不存在」的存量库上跑。

| 文件 | 改动 |
|---|---|
| `src/stockmaster/db/models.py` | `WatchlistItem` / `AlertRule` / `AlertTrigger` / `BacktestRun` 加 `owner_id: int = 0`；唯一约束改 `(owner_id, list_name, code)`；新增 4 个复合索引 |
| `src/stockmaster/alembic/versions/0002_owner_columns.py` | 四张表 `add_column`（`NOT NULL DEFAULT 0`）；`watchlist_items` 用 `batch_alter_table` 重建唯一约束；`alert_triggers.owner_id` 从 `alert_rules` 回填 |
| `src/stockmaster/core/auth_deps.py` | 新增 `current_owner()`：登录用户取 `user.id`，匿名回落 `GUEST_OWNER_ID = 0` |
| 各 `create_*` | 显式写 `owner_id=owner`（不依赖模型默认值 0） |
| `src/stockmaster/cli.py` | `stockmaster user claim --username <name> [--dry-run]`：把 owner 0 的历史数据交给某个账号 |
| `src/stockmaster/config.py` | `data_isolation: bool = False`（灰度开关，默认关 = 现状） |

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

### PR-2：读路径隔离 —— ✅ 已完成

> 落地后的名字：`owned()` / `get_owned_or_404()`（`stockmaster/db/scoped.py`）、
> `current_owner`（`stockmaster/auth/deps.py`）。开关关闭时两个助手都退化成空操作，
> 所以 `SM_DATA_ISOLATION=false` 时请求路径与升级前逐字节一致。

| 文件 | 改动 |
|---|---|
| `src/stockmaster/db/scoped.py`（新增） | `owned_stmt()` / `owned_or_404()`：非本人一律 **404**（403 会泄露 id 是否存在） |
| `api/v1/system.py` | 自选股 4 个端点按 owner 过滤 |
| `api/v1/alerts.py` | 规则/触发/扫描/预览按 owner；`/alerts/scan` 只扫本人 |
| `api/v1/backtest.py` | 历史与详情按 owner |
| `alert/engine.py` | `_resolve_scope` 的 `watchlist` 分支按 `(rule.owner_id, list_name)` 取；**这是最容易漏的一行** |
| `jobs/tasks.py` | `_default_universe()` / `job_snapshot_poll` 取所有用户自选的**并集**（共享行情数据只存一份） |
| `frontend` | 基本无改动；用户页可加只读统计列（自选数/规则数/回测数） |

**验收（= 测试矩阵，`tests/test_isolation.py`）**

两个账号 A/B 对每个私有端点交叉访问，断言 404 或空列表：

```python
PRIVATE_CASES = [
    ("GET", "/api/v1/watchlist"),
    ("DELETE", "/api/v1/watchlist/{a_item}"),
    ("GET", "/api/v1/alerts/rules"),
    ("PATCH", "/api/v1/alerts/rules/{a_rule}"),
    ("GET", "/api/v1/alerts/triggers"),
    ("GET", "/api/v1/backtest/{a_run}"),
]
```

另外必须覆盖：两人同名 `default` 清单各加同一只股票都成功（旧约束会串改 `note`）；
B 的 `scope=watchlist` 规则只命中 B 的自选；匿名（`SM_AUTH_ENABLED=false`）落 owner 0 可读写。

**回滚**：`SM_DATA_ISOLATION=false` 立即回到共享语义（列还在，无害）。

### PR-3：升级为工作区隔离（owner/editor/viewer）—— ✅ 已完成

> 设计细节见 `WORKSPACE.md`。核心变化：归属列 `owner_id` 改名并重新解释为
> `workspace_id`，账号模型换成 cndb 式 `Workspace` + `WorkspaceMember(role)`。

| 文件 | 改动 |
|---|---|
| `src/stockmaster/alembic/versions/0003_workspaces.py` | 幂等迁移：`owner_id` 改名 `workspace_id`、建 `workspaces`/`workspace_members`、按用户回填个人工作区、重建索引与 `uq_watchlist_item` |
| `src/stockmaster/db/models.py` | `WORKSPACE_UNASSIGNED = 0`；Workspace / WorkspaceMember 模型 |
| `src/stockmaster/plugins/workspaces/`（新增） | `roles.py`（ROLE_RANK / at_least，未知角色拒绝）、`service.py`（成员管理、最后 owner 保护、所有权转让、认领）、`deps.py`（`current_workspace` / `require_editor`，解析 `X-Workspace-Id`） |
| `src/stockmaster/db/scoped.py` | `owned_*` 重写为 `in_workspace()` / `get_in_workspace_or_404()` |
| `api/v1/{system,alerts,backtest}.py` | 读端点 `current_workspace`、写端点 `require_editor`；`/alerts/preview` 保持只读可用 |
| `alert/engine.py` / `jobs/tasks.py` | watchlist 按 `(rule.workspace_id, list_name)` 解析；调度器取全站并集 |
| `api/v1/workspaces.py`（新增） | 工作区与成员 CRUD、候选列表、所有权转让 |
| `src/stockmaster/cli.py` | `stockmaster workspace list/create/add/role/remove/transfer/claim`；`user claim` 委托为个人工作区认领 |
| frontend | zustand workspace store + `X-Workspace-Id` 头 + 切换器 + `/workspace` 成员管理页 + viewer 只读态 |

**验收**：`tests/test_workspace.py`（角色矩阵：viewer 403 + `X-Auth-Error: read_only`、
editor 不可管成员、最后 owner 保护、跨工作区 404、转让）；`tests/test_isolation.py`
重写为两个工作区交叉访问；存量库模拟验证 0003 回填正确、`schema_drift()` 为空。

**回滚**：`SM_DATA_ISOLATION=false`（迁移不回滚 —— 列名变化无害，旧代码读不到
`owner_id` 才是问题，因此代码回滚需连 `git revert` 一起做）。

### PR-4：工作区级通知通道 —— ✅ 已完成

> 设计细节见 `WORKSPACE.md` §5.1。核心变化：预警外发从「全站一个 webhook」变成
> 「规则属于哪个工作区，就发到哪个工作区配置的通道」；全局 webhook 只服务
> `workspace_id=0` 或显式 fallback。

| 文件 | 改动 |
|---|---|
| `src/stockmaster/alembic/versions/0005_workspace_channels.py` | 幂等建 `workspace_channels`：`workspace_id` FK（CASCADE）、`(workspace_id, name)` 唯一、`(workspace_id, enabled)` 索引 |
| `src/stockmaster/db/models.py` | `WorkspaceChannel` 模型（kind=webhook/bark、url 含凭据、enabled） |
| `src/stockmaster/config.py` | `SM_ALERT_GLOBAL_FALLBACK`（默认 false = 严格隔离） |
| `src/stockmaster/plugins/workspaces/{services/service.py,schemas/workspace.py,routers/workspaces.py}` | 通道 CRUD 服务 + 校验 + URL 掩码；`/workspaces/{id}/channels` 五个端点（写仅 owner） |
| `src/stockmaster/plugins/alerts/services/channels.py` | `Notification.workspace_id`、`channels_for_workspace()`、`build_channel()`、`send_test()`；`notify()` 按工作区分组路由 |
| `src/stockmaster/jobs/tasks.py` | `job_scan_alerts` 把触发所属 `workspace_id` 透传到 `Notification` |
| frontend | 「工作区」页新增通知通道区块：成员只读（地址脱敏），owner 增删改/测试；无通道提示不外发 |
| `.env.example` / `docker-compose.prod.yml` | 同步 `SM_ALERT_GLOBAL_FALLBACK`（compose 不会自动透传 `.env`） |

**验收**：`tests/test_workspace_channels.py`（仅 owner 可写、跨工作区 404、URL 掩码、
重名 409、测试端点）；`tests/test_alert_notify.py`（隔离关闭保持旧行为、两个工作区
各收各的、停用/无通道/fallback、workspace 0 走全局、job 透传工作区）。

**回滚**：不配置任何通道 + 保持 `SM_ALERT_GLOBAL_FALLBACK=false` 即不外发；需要恢复
旧的全站推送时，配置 `SM_ALERT_WEBHOOK_URL` 并置 `SM_ALERT_GLOBAL_FALLBACK=true`，
或直接回退代码（`0005` 建的表可安全保留，旧代码不读它）。

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
# 1) 拉代码（本次新增了 alembic.ini、src/stockmaster/alembic、stockmaster/maintenance，必须重建镜像）
cd /opt/stockmaster && git pull

# 2) 部署前先看清当前库版本与数据量
docker compose --env-file .env -f docker-compose.prod.yml exec app \
  python -m stockmaster.cli db history    # 升级前通常显示「未纳管」

# 3) 构建并发布（deploy.sh 会自动备份 /data 卷，保留 5 份）
./deploy/deploy.sh

# 4) 确认启动时已自动认领版本并执行增量
docker compose --env-file .env -f docker-compose.prod.yml exec app \
  python -m stockmaster.cli db current      # 应输出 当前=最新=0005_workspace_channels
docker compose --env-file .env -f docker-compose.prod.yml exec app \
  python -m stockmaster.cli db check        # 应无「缺列」输出

# 5) 手工打一份基线备份（之后的任何操作都有退路）
docker compose --env-file .env -f docker-compose.prod.yml exec app \
  python -m stockmaster.cli backup --note "alembic-introduced"
```

演练恢复（**先在本地/临时数据目录做**）：

```bash
SM_DATA_DIR=/tmp/drill python -m stockmaster.cli restore ~/backups/xxx.tar.gz --dry-run
SM_DATA_DIR=/tmp/drill python -m stockmaster.cli restore ~/backups/xxx.tar.gz --force
```

### 需要的配置项（都有默认值，可不动）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SM_DB_AUTO_MIGRATE` | `true` | 启动自动迁移；设 false 则要求先 `db upgrade`（版本落后会告警） |
| `SM_BACKUP_KEEP` | `7` | 本地备份保留份数，`0` 不清理 |
| `SM_BACKUP_INCLUDE_SECRET` | `true` | 归档是否含 `.auth_secret` |
| `SM_DATA_ISOLATION` | `false` | 工作区隔离（owner/editor/viewer）；`false` = 共享语义 |

> `docker-compose.prod.yml` 逐项列出 environment，新增变量**必须**同步进去，否则传不进容器。
> 以上（含 `SM_DATA_ISOLATION`）已逐项写进 `docker-compose.prod.yml` 的 environment。
> compose 不会自动透传 `.env`，新增变量必须同步进去，否则容器里只会拿到默认值。

---

## 6. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 镜像漏拷 `alembic.ini` / `migrations` | 容器内 `db` 命令报「迁移目录缺失」 | Dockerfile.prod 已加 `COPY alembic.ini ./`；`src/stockmaster/alembic` 随 `COPY src/` 进入 |
| 存量库被误 `stamp` 到 head 但结构落后 | 少了列，业务查询才报错 | `ensure_migrated` 之后跑 `schema_drift()` 告警；`db check` 可显式体检 |
| 恢复期间仍有 HTTP 写请求 | 数据竞争 | 恢复前暂停调度器 + 进程内互斥锁；**完成后建议重启服务**（响应里已提示） |
| 备份含签名密钥 | 归档泄露 = 可伪造会话 | manifest 标注 `secrets.auth_secret`，检查页与列表都提示；可 `--no-secret` / `include_secret=false` |
| 全市场库备份体积大 | 50 GB 磁盘被备份吃满 | `SM_BACKUP_KEEP` 限制份数；deploy.sh 的卷备份另有 5 份上限；必要时降 `keep` |
| 同一秒两次备份 | 后一份覆盖前一份 | 已修：`_unique_path()` 自动追加 `-2`、`-3` |
| SQLite 在 Windows 上文件被占用 | 迁移/清库失败 | 备份恢复一律走连接协议而非删文件；测试里显式 `reset_engine()` |

---

## 7. 未决项

1. 工作区是否需要「共享清单」可见性细分（当前：同工作区成员全量共享，角色只分读写）。
2. 回测结果是否支持发链接共享：需要 `visibility` + `share_token`，而不是放宽工作区过滤。
3. 备份是否异地留存（COS）：等本地轮转稳定运行一段时间再评估。
