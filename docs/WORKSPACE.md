# 工作区与角色权限设计

> 状态：**已实施**（`0003_workspaces` 迁移 + Workspace/WorkspaceMember 表 + 读写路径改造 +
> 角色矩阵测试；`0005_workspace_channels` 起支持按工作区配置通知通道，见 §5.1）。
> 开关：`SM_DATA_ISOLATION`，默认 `false`（= 无工作区概念的共享语义），可随时回退。
> 前置：账号体系（`src/stockmaster/plugins/auth/`）已落地。
> 本文取代 `docs/USER_ISOLATION.md` 的设计（owner_id 用户级隔离 → cndb 式工作区隔离），
> 设计取舍仍大量沿用前文，冲突处以本文为准。

---

## 1. 为什么从「按账号」改成「按工作区」

用户级隔离（每人一份私有数据）解决的是个人隐私，解决不了**协作**：

- 团队共用一套自选池/预警规则时，按账号隔离反而制造数据孤岛，只能靠复制粘贴同步；
- 「谁能看」和「谁能改」是两个问题，账号模型里没有位置放第二个答案；
- 数据归属列写 `owner_id` 后，把语义从「属于某个用户」放宽为「属于某个工作区」是纯列语义
  变更，不需要再加列 —— 这是当初坚持命名 `owner_id`（而不是 `user_id`）就预留的口子。

因此本版把四张私有表的归属列 `owner_id` **重命名并重新解释为 `workspace_id`**，
参考 cndb 项目（`F:\Dev\cndb\src\cndb\plugins\workspaces\`）的工作区模型：
`Workspace` + `WorkspaceMember(user_id, role)`，角色三级 `owner / editor / viewer`。

## 2. 角色模型

```python
# src/stockmaster/plugins/workspaces/services/roles.py
ROLE_RANK = {"owner": 3, "editor": 2, "viewer": 1}
GRANTABLE_ROLES = {"editor", "viewer"}  # owner 只能通过「转让」产生
```

| 角色 | 读数据 | 写数据（自选/规则/回测/扫描） | 管理成员 | 改名/删除工作区 |
|---|---|---|---|---|
| owner | ✅ | ✅ | ✅ | ✅ |
| editor | ✅ | ✅ | ❌（只能邀 editor/viewer） | ❌ |
| viewer | ✅ | ❌（403 + `X-Auth-Error: read_only`） | ❌ | ❌ |

关键语义（与 cndb 对齐）：

- **`at_least(role, min)` 对未知角色返回 `False`**，不退化成 viewer 再比较 ——
  否则拼错的角色名会意外拿到最低权限而非被拒绝；
- **最后一个 owner 不可移除/降级**（`_guard_last_owner`）；移除工作区里的唯一 owner
  之前必须先转让；
- **所有权转让**：新 owner 确认后，原 owner 自动降为 editor（cndb 同款行为，
  避免出现「两个 owner」或「零个 owner」）；
- **owner 不可通过 add_member 直接产生**：`GRANTABLE_ROLES` 只有 editor/viewer。

## 3. 数据归属

### 列语义变更

```python
# src/stockmaster/db/models.py
WORKSPACE_UNASSIGNED = 0  # 原 OWNER_UNASSIGNED

# 四张私有表统一（0003 迁移里由 owner_id 改名而来）
workspace_id: Mapped[int] = mapped_column(Integer, default=WORKSPACE_UNASSIGNED, nullable=False)
```

- `workspace_id = 0` 仍是「未归属」桶：迁移前的历史数据、匿名会话写入的数据。
  不指向任何工作区行，因此该列依旧**不加外键**。
- `backtest_equity / backtest_trades` 仍只靠 `run_id` 级联，不冗余归属列。
- `alert_triggers` 仍冗余 `workspace_id`（从 rule 复制，高频列表查询不回 join）。

### 唯一约束与索引

| 表 | 改为 |
|---|---|
| `watchlist_items` | `uq_watchlist_item (workspace_id, list_name, code)` |
| 索引 | `ix_alert_rules_workspace`、`ix_alert_triggers_workspace`、`ix_backtest_runs_workspace` |

同名清单同一只股票，现在允许在不同工作区各存一份（之前按用户也各存一份）。

选股 / 样本 / 算法三类策略配置同样是工作区私有数据（`workspace_id` 归属同上、工作区内
`name` 唯一），它们如何被命名复用、谁能运行与训练见
[`docs/STRATEGY_CONFIGS.md`](STRATEGY_CONFIGS.md)。

### 共享参考数据不动

行情/指标/模型（类别 A）与同步任务/审计（类别 B）仍然全局一份。
**原则不变：共享参考数据只有一份，工作区只决定「关注哪些」和「什么时候提醒我」。**

## 4. 权限分层怎么落实

沿用「显式作用域 + 测试矩阵」方案，入口从 `auth/deps.current_owner` 换成
`workspace/deps`：

```python
# src/stockmaster/core/workspace_deps.py
@dataclass
class WorkspaceCtx:
    workspace: Workspace
    role: str            # 当前账号在该工作区的角色

def current_workspace(...) -> WorkspaceCtx:
    """解析 X-Workspace-Id 请求头指定的工作区；缺省回落个人工作区。
    非成员 / 不存在 → 404（不泄露存在性）。隔离关闭时返回 None。"""

def require_editor(...) -> WorkspaceCtx:
    """写端点专用：viewer → 403，响应头 X-Auth-Error: read_only。"""
```

端点分层（`src/stockmaster/plugins/<域>/routers/`）：

| 端点 | 依赖 | viewer 结果 |
|---|---|---|
| `GET /watchlist`、`GET /system/status` | `current_workspace` | ✅ 可读 |
| `POST/DELETE /watchlist*` | `require_editor` | 403 |
| `GET /alerts/rules`、`GET /alerts/triggers` | `current_workspace` | ✅ |
| `POST/PATCH/DELETE /alerts/rules*`、`PATCH /alerts/triggers/{id}`、`POST /alerts/scan` | `require_editor` | 403 |
| `POST /alerts/preview` | `current_workspace` | ✅（刻意只读，viewer 可试用规则） |
| `GET /backtest/history`、`GET /backtest/{id}` | `current_workspace` | ✅ |
| `POST /backtest/run` | `require_editor` | 403（回测吃 CPU） |

跨工作区访问（拿 A 工作区的 id 换 B 工作区的 `X-Workspace-Id` 头）一律 404，
由 `get_in_workspace_or_404()` 保证 —— 404 而非 403 是为了不泄露 id 是否存在。

## 5. 调度器与无用户上下文

| 任务 | 处理方式 |
|---|---|
| 预警扫描 | `scope_type=watchlist` 时按 `(rule.workspace_id, list_name)` 解析自选（`AlertEngine._resolve_scope`） |
| 每日同步 `_default_universe(workspace_id=None)` | `None` = 取**全站自选并集**（`DISTINCT code`）——调度器服务所有工作区；手动触发按单工作区取 |
| 通知投递 | 按工作区路由：规则属于哪个工作区，就发到哪个工作区配置的通道（§5.1）。全局 `SM_ALERT_WEBHOOK_URL` 只用于 `workspace_id=0` 的未归属数据，或显式开启 `SM_ALERT_GLOBAL_FALLBACK` 时兜底 |

### 5.1 工作区级通知通道（`0005_workspace_channels`）

**问题**：隔离做到读写路径之后，通知出口仍是全站一个 `SM_ALERT_WEBHOOK_URL`。两个工作区同时触发预警时，提醒会混进同一个群机器人 —— A 组的股票代码、阈值和触发消息出现在 B 组的频道里。这不是显示问题，而是隔离在最后一步漏了。

**数据模型**：`workspace_channels`

| 列 | 语义 |
|---|---|
| `workspace_id` | FK → `workspaces.id`，`ON DELETE CASCADE`。通道**只属于真实工作区**：不存在 `workspace_id=0` 的「未归属通道」，隔离关闭时也不会写这张表 |
| `name` | 成员识别「这条发到哪」的入口；`(workspace_id, name)` 唯一 |
| `kind` | `webhook`（通用 JSON POST，适合钉钉/企微/Server酱中转）、`bark`（iOS 推送）或 `email`（SMTP 邮件） |
| `url` | webhook token / Bark device key / 邮件收件人（email 时填地址，逗号分隔）；API **只返回掩码**，更新时省略 `url` 表示保持原值 |
| `enabled` | 停用的通道留在列表里但不参与投递 |

**投递路由**（`plugins/alerts/services/channels.py`）：

| 场景 | 投递目标 |
|---|---|
| `SM_DATA_ISOLATION=false` | 全局通道 = database sink + `SM_ALERT_WEBHOOK_URL`，与引入工作区之前完全一致 |
| 隔离开启，`workspace_id > 0` | database sink + 本工作区所有 `enabled` 通道；一个启用的都没有 → 不外发（严格隔离，默认） |
| 隔离开启，工作区无通道且 `SM_ALERT_GLOBAL_FALLBACK=true` | 追加全局 webhook 兜底（给「先开隔离、通道后配」的过渡期留的开关） |
| 隔离开启，`workspace_id = 0` | 全局通道 —— 未归属的历史触发记录本来就不属于任何工作区 |

`notify()` 按 `workspace_id` 分组解析通道，返回的投递计数在隔离开启时带 `ws{id}:` 前缀（例如 `ws3:webhook`），便于排查「哪个工作区发出去了」。database sink 始终存在，所以即使没有任何外发通道，触发记录也不会丢。

**权限**：通道是**数据出口**，写操作（增删改、发测试消息）仅 `owner`；任何成员可读列表，但 URL 一律脱敏。让 editor 能改通道等于允许他把工作区数据转发到任意地址，因此不随「可写数据」一起下放。接口：

| 方法 | 路径 | 依赖 |
|---|---|---|
| `GET` | `/api/v1/workspaces/{id}/channels` | 成员（`path_workspace`），返回掩码 |
| `POST` | `/api/v1/workspaces/{id}/channels` | `require_workspace_owner`，201 |
| `PATCH` | `/api/v1/workspaces/{id}/channels/{channel_id}` | `require_workspace_owner` |
| `DELETE` | `/api/v1/workspaces/{id}/channels/{channel_id}` | `require_workspace_owner`，204 |
| `POST` | `/api/v1/workspaces/{id}/channels/{channel_id}/test` | `require_workspace_owner`；网络失败返回 `delivered=false`，不抛 500 |

跨工作区拿 `channel_id` 访问一律 404（与其他私有资源同一约定）；同工作区重名 409。

**前端**：「工作区」页新增「通知通道」区块：成员看到名称/类型/脱敏地址/启停状态，owner 追加增删改与「测试」按钮。没有通道时会提示「本工作区触发预警时不会外发」。隔离关闭时整页仍是「未启用数据隔离」说明，不出现该区块。

**迁移与开关**：`0005_workspace_channels` 幂等建表（`create_all` 已建则跳过），不改任何既有表；不配置通道时升级后行为不变。`SM_ALERT_GLOBAL_FALLBACK` 默认 `false`。


### 5.2 工作区用量统计与可选配额（无 schema 变更）

**为什么拆成只读统计 + 默认关闭的配额**
隔离解决「谁能看见谁的数据」，配额解决「一个工作区能放多少」。两者都应该在工作区层回答，但风险不同：统计只是读，可以默认开启；配额会拒绝写请求，默认必须关闭，否则升级后单机用户会突然被一个自己没配过的上限卡住。

**用量统计**：`GET /api/v1/workspaces/{id}/stats`（`path_workspace`，成员可读；管理员凭救援语义同样可读）

| 字段 | 说明 |
|---|---|
| `usage` | 8 类用量：成员、自选股、预警规则、回测记录、选股/样本/算法配置、通知通道 |
| `limits` | 与 `usage` 同键的当前上限；`0` = 不限 |
| `labels` | 资源的中文名，前端直接展示，避免在 TS 里维护第二份字典 |
| `total` | 上述 8 类用量之和（注意包含成员与通道，不是「数据行数」） |

只统计工作区私有数据 + 成员 + 通道；行情、指标、因子、模型是全站共享参考数据，不计入任何工作区的用量。前端「工作区」页新增「用量与配额」卡片，所有成员可见（只读）。

**配额**（`core/quota.py`，环境变量见 §10，全部默认 `0` = 不限）：

| 资源 | 校验时机 |
|---|---|
| `members` | owner 邀请成员前 |
| `watchlist_items` | 新增自选前（**幂等更新已有条目不占额度**） |
| `alert_rules` | 新建规则前（参数 422 校验优先） |
| `backtest_runs` | 回测请求进入 CPU 计算前 |
| `screening_configs` / `sample_configs` / `algorithm_configs` | 三类配置共用 `SM_WORKSPACE_MAX_STRATEGY_CONFIGS`，**按表分别计数**（各可建 N 条） |
| `channels` | owner 新建通知通道前 |

拒写行为：`409` + 响应头 `X-Auth-Error: quota_exceeded`，`detail` 含资源名与上限。关键语义：

- **隔离关闭时不生效**（`workspace_id = 0` 的未归属桶）——单机模式没有工作区概念，保持引入工作区前的行为；
- **默认全 0 = 不限制**，配置校验拒绝负数；
- 配额是治理手段而不是精确计费：检查与写入之间没有加锁，并发请求可能短暂超出上限 1～2 条，可接受；
- 删除数据立即释放额度（计数是实时的，不缓存）；
- 统计/配额不新增表、不改迁移，升级只需重启。

**回滚**：把所有 `SM_WORKSPACE_MAX_*` 置回 `0`（或不设置）即完全恢复无配额行为；统计端点可保留，不影响任何写路径。

**用户页与管理员统计列**（同一数据口径，无新表/新端点）：

- `WorkspaceOut.data_total`：每个工作区的「数据量」——自选 + 预警规则 + 回测 + 三类策略配置，**不含成员与通知通道**。`GET /workspaces` 会把「我参与的工作区」连同 data_total 一次返回（批量 GROUP BY，不做 N×6 查询），用户页据此展示「参与工作区数 / 个人工作区数据量 / 可访问数据合计」。
- 管理员 `GET /users` 的每条用户额外返回：`workspace_count`（参与工作区数）、`owned_workspace_count`（`workspaces.owner_id` 计数，含个人工作区）、`personal_data_total`（其 personal 工作区的 data_total）。管理员用户列表直接渲染为统计列。
- 共享工作区的数据计入「可访问数据合计」，但不计入任何用户的 `personal_data_total` —— 它属于工作区，不属于某个账号。
- 隔离关闭时 `/workspaces` 为空、用户页不展示概览；管理员列退化为 0。

### 5.3 工作区级预警参数覆盖（`0006_workspace_alert_policies`）

**问题**：单条规则自己可以设排除 ST、成交额门槛、冷却与每日一次，但「团队层面的提醒底线」没有位置放。owner 想让本工作区所有提醒都排除 ST、成交额低于 X 不提醒，只能逐条改规则 —— 既容易漏，又会被后来新增的规则绕过。

**语义：只收紧、不放宽**。策略与规则的有效值按「取更严格者」计算：

| 维度 | 有效值 | 说明 |
|---|---|---|
| `exclude_st` | `rule.exclude_st OR policy.exclude_st` | 任一为 True 即排除 ST |
| `min_amount` | `max(rule.min_amount, policy.min_amount)` | 策略只能抬高成交额门槛 |
| `cooldown_seconds` | `max(rule.cooldown_seconds, policy.cooldown_seconds)` | 策略只能延长冷却 |
| `once_per_day` | `rule.once_per_day OR policy.once_per_day` | 任一为 True 即每天最多一次 |

因此策略不会成为绕过规则设置的后门：规则自己更严格时以规则为准。

**数据模型**：`workspace_alert_policies`（一个工作区最多一行）

| 列 | 语义 |
|---|---|
| `workspace_id` | 主键 + FK → `workspaces.id`（`ON DELETE CASCADE`）；不存在 `workspace_id=0` 的策略行 |
| `exclude_st` / `min_amount` / `cooldown_seconds` / `once_per_day` | 可空；`NULL` = 该维度跟随规则；无行 = 全部跟随规则 |
| `updated_by` / `updated_at` | 审计用 |

**API**（`plugins/workspaces/routers/workspaces.py`）：

| 方法 | 路径 | 依赖 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/workspaces/{id}/alert-policy` | `path_workspace`（成员可读） | 返回四个覆盖值（null=跟随规则）+ `effective` 只读有效值 |
| `PUT` | `/api/v1/workspaces/{id}/alert-policy` | `require_workspace_owner` | upsert；字段传 null = 清除该维度；四个都 null = 删除行恢复默认 |

写权限只给 owner：策略会影响整个工作区的提醒，属于团队治理而不是「可写数据」。校验：`min_amount >= 0`、`cooldown_seconds` 在 `0..30 天` 内。

**生效点**：`AlertEngine.evaluate_all()` 在扫描开始时按工作区批量取策略（`plugins/alerts/services/policy.py::effective_policies`），过滤与 `dedup_key` 都用有效值。调度器扫描与手动 `/alerts/scan` 自动生效；`/alerts/preview` **不套用**策略 —— 它只按用户输入的规则预演匹配。策略变更从下一次扫描起生效，历史触发记录的 `dedup_key` 不重写。

**迁移与清理**：`0006_workspace_alert_policies` 幂等建表；删除工作区时显式清理策略行（不计入 `data_counts`，与通道一致）。`workspace claim` 不涉及策略（策略只属于真实工作区）。

**回滚**：把 `workspace_alert_policies` 行删除（或 PUT 全 null）即恢复「全部跟随规则」，不影响既有触发记录；迁移表可安全保留。

### 5.4 P3 可选增强：邮件通知、消息模板、用量趋势与导出

隔离路线图收尾后的体验增强，全部为可选配置，默认不改变既有行为。

**邮件通知通道**：通道 `kind=email`，`url` 列存**收件人**（逗号/分号分隔，最多 10 个，API 掩码展示如 `a***@example.com`）。SMTP 是部署级凭据，走环境变量 `SM_SMTP_*`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SM_SMTP_HOST` | 空 | 留空 = 邮件通道不投递（创建/测试返回 `delivered=false`，不抛错） |
| `SM_SMTP_PORT` | `587` | SMTP 端口 |
| `SM_SMTP_USERNAME` / `SM_SMTP_PASSWORD` | 空 | 有用户名时登录 |
| `SM_SMTP_FROM` | 空 | 发件人；留空用 `SM_SMTP_USERNAME` |
| `SM_SMTP_TLS` | `true` | 是否 `STARTTLS` |
| `SM_SMTP_TIMEOUT` | `10` | 连接/发送超时（秒） |

投递实现用标准库 `smtplib`（无新增依赖）；网络失败只 log warning，与 webhook/Bark 一致。

**按工作区消息模板**：`workspaces.message_template`（`0007` 新增列）。空 = 默认正文；非空时对**本工作区**的每条外发通知套用 `str.format` **重写正文**（标题保持原样，邮件 Subject / Bark 标题需要短标题），占位符只允许 `{title} {body} {code} {rule_id} {level}`。API 保存时校验非法花括号/未知字段（422 `invalid_template`），运行期格式化失败会 log 并回退原文；`workspace_id=0` 不套模板。owner 在「工作区设置」编辑。

**用量趋势与导出**（`workspace_usage_snapshots`，`0007` 新增表）：

| 项 | 说明 |
|---|---|
| 快照 | 一行/工作区/天，`(workspace_id, captured_on)` 唯一；`captured_on` 同日重复执行只更新（upsert），不产生重复点 |
| 写入 | 调度任务 `workspace_usage`（交易日 17:30，`job_snapshot_workspace_usage`）；也可用 `POST /system/jobs/workspace_usage` 手动跑 |
| 查询 | `GET /api/v1/workspaces/{id}/stats/history?days=30`（成员可读，1..365 天，按日期升序） |
| 导出 | `GET /api/v1/workspaces/{id}/stats/export`（成员可读）→ `text/csv`，UTF-8 BOM + `Content-Disposition` 附件；包含 8 类资源用量与上限 |

前端「用量与配额」卡片新增「导出 CSV」与 30 天趋势折线（数据量 / 成员数）；快照从任务首次运行开始积累，历史为空时显示提示。

**迁移与回滚**：`0007_workspace_email_and_usage_history` 幂等加列/建表；回滚只需删除快照表与模板列（或保留两者，旧代码不读）。邮件通道不配置 `SM_SMTP_HOST` 时完全不外发。

## 6. 迁移：`0003_workspaces`

幂等设计 —— 同一个迁移既要在**全新库**（列建出来就叫 `workspace_id`）跳过改名，
又要在 **0002 存量库**（还是 `owner_id`）上执行：

1. 探测列名：`owner_id` 存在 → `ALTER TABLE ... RENAME COLUMN owner_id TO workspace_id`
   （batch 模式）；已是 `workspace_id` → 跳过；
2. 建表 `workspaces` / `workspace_members`（`create_all` 之外仍要显式建，
   保证「先升级迁移再启动」的库也有表）；
3. **按用户回填个人工作区**：每个有私有数据的用户自动获得 `kind=personal` 工作区，
   其数据 `workspace_id` 指向它 —— 存量用户升级后数据"原地搬家"，无感；
4. 重建索引与 `uq_watchlist_item`（batch 模式自动走建新表→拷贝→改名序列）；
5. 无主数据（原 owner 0）保持 `workspace_id = 0`，用 CLI 认领。

存量库的纳管仍走既有约定：`stamp` 认领 baseline → `upgrade head`，
**不要**在升级链里重放历史迁移（`ensure_migrated()` 已处理，勿回退）。

## 7. CLI

```bash
stockmaster workspace list                       # 我参与的工作区（--all 管理员看全部）
stockmaster workspace create --name 团队池
stockmaster workspace members --id 2
stockmaster workspace add --id 2 --user bob --role editor
stockmaster workspace role --id 2 --user bob --role viewer
stockmaster workspace remove --id 2 --user bob
stockmaster workspace transfer --id 2 --user bob # 转让所有权，原 owner 降 editor
stockmaster workspace claim --id 2 --dry-run     # 把 workspace_id=0 的数据移交该工作区
# 兼容：user claim --username X 现在委托为「确保个人工作区 + 认领到该工作区」
```

## 8. 前端

- `src/workspace.ts`：zustand store，`refresh()` 拉工作区列表，`select()` 切换；
  localStorage `sm.workspace.id` 记住选择；
- `src/api.ts`：所有请求（request/requestRaw）自动注入 `X-Workspace-Id` 头；
- `src/App.tsx`：顶栏 `WorkspaceBar` 切换器（viewer 显示琥珀色只读徽章）；
  `<Routes key={currentId ?? "local"}>` —— 换工作区 = 整树重挂载，所有 `useQuery` 自动重取，
  杜绝「数据没跟着切」；
- `canWriteWorkspace()`：当前工作区角色为 viewer → 各页面禁用写按钮
  （回测的「开始回测」、预警的扫描/创建/启停/删除/已读静音），
  并显示只读提示；`/alerts/preview` 对 viewer 保持可用；
- `/workspace` 页面：owner 管理成员（邀请、改角色、移除、转让）；
  隔离关闭时该页显示「未启用数据隔离」说明，切换器不出现。
- `/account` 页面：「我的数据概览」展示参与工作区数、个人工作区数据量、可访问数据合计，
  以及每个工作区的角色/成员数/数据量（只读）；
- `/users` 页面（管理员）：用户列表新增「工作区」「个人数据量」两列；
- `/workspace` 页面（管理员）：「全部工作区」总览卡片（名称/类型/成员/数据量）；
- `/workspace` 页面「预警策略」卡片：成员只读展示有效值，owner 可配置
  `exclude_st` / `min_amount` / `cooldown_seconds` / `once_per_day`（null = 跟随规则）；
- `/workspace` 页面「工作区设置」：owner 可编辑预警消息模板（占位符提示）；
  通知通道类型支持 Webhook / Bark / 邮件，邮件时输入收件人；
- 用量卡片支持「导出 CSV」与 30 天趋势折线（快照为空时显示积累提示）。

## 9. 测试矩阵

- `tests/test_workspace.py`：角色矩阵 —— viewer 写 403 + `X-Auth-Error: read_only`；
  editor 不可管成员；最后一个 owner 保护；跨工作区 404；转让后角色互换；
  preview 对 viewer 200；
- `tests/test_isolation.py`：重写为工作区语义 —— 两个工作区对每个私有端点交叉访问，
  参数化断言「404 或空列表，绝不返回对方数据」；同名清单同码股票在两个工作区互不冲突；
  预警规则只命中本工作区自选；匿名 + 隔离关闭落 workspace 0。
- `tests/test_workspace_channels.py`：通道 CRUD 与角色矩阵 —— 仅 owner 可增删改/测试，
  editor/viewer 403，非成员/跨工作区 404，URL 掩码不回显密钥，重名 409；
- `tests/test_alert_notify.py`：按工作区投递 —— 隔离关闭保持旧 key 与全站 webhook；
  两个工作区各收各的通知；停用/无通道/`SM_ALERT_GLOBAL_FALLBACK` 的三种分支；
  `workspace_id=0` 走全局；`job_scan_alerts` 把触发所属工作区透传到 Notification。
- `tests/test_workspace_stats_quota.py`：用量统计（usage/limits/labels/total、成员可读、
  非成员 404）与超限矩阵 —— 自选/规则/通道/成员/策略配置超限均 409 +
  `X-Auth-Error: quota_exceeded`；删除后释放额度；`SM_WORKSPACE_MAX_*=0` 或隔离关闭时不限制。
- `tests/test_user_stats.py`：用户页/管理员统计 —— `GET /workspaces` 的 data_total 口径
  （不含成员/通道）、`GET /users` 三个统计字段、权限（非管理员 403）、`q` 过滤后统计正确、
  `quota.data_totals` 批量计算与未知 id 补 0。
- `tests/test_workspace_alert_policy.py`：预警策略 API —— 默认值/持久化/全 null 重置、
  仅 owner 可写（editor/viewer 403）、参数校验 422、删除工作区清理策略行、
  `effective_policy()` 对 NULL/脏数据的归一。
- `tests/test_alert_policy_engine.py`：策略在扫描引擎生效 —— exclude_st/min_amount 过滤、
  cooldown/once_per_day 收紧 `dedup_key`、跨工作区互不影响、规则更严格时以规则为准。
- `tests/test_notification_email.py`：邮件通道（smtplib 打桩：无配置/异常返回 False、
  STARTTLS/登录/收件人正确）+ 通道 API（email 掩码、非法收件人 422、角色矩阵）+
  消息模板（保存/校验 422/notify 套用/workspace 0 不套用）。
- `tests/test_workspace_usage_history.py`：CSV 导出（BOM/表头/数量/附件名/权限）、
  趋势历史（空列表、任务生成、同日 upsert、days 过滤、升序）、多工作区快照与级联清理。

## 10. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `SM_DATA_ISOLATION` | `false` | `false` = 无工作区概念，全员共享（旧语义，单机推荐）；`true` = 启用工作区隔离与角色权限 |
| `SM_ALERT_WEBHOOK_URL` | 空 | 全局外发 webhook。隔离关闭时是唯一外发通道；隔离开启时只服务 `workspace_id=0` 或 fallback |
| `SM_ALERT_GLOBAL_FALLBACK` | `false` | 隔离开启时，没有配置任何启用通道的工作区是否回落到全局 webhook；默认 `false` = 严格隔离 |
| `SM_SMTP_HOST` | 空 | 邮件通道 SMTP 主机；留空 = 邮件通道不投递 |
| `SM_SMTP_PORT` | `587` | SMTP 端口 |
| `SM_SMTP_USERNAME` / `SM_SMTP_PASSWORD` | 空 | SMTP 登录凭据 |
| `SM_SMTP_FROM` | 空 | 发件人；留空用 `SM_SMTP_USERNAME` |
| `SM_SMTP_TLS` | `true` | 是否 STARTTLS |
| `SM_SMTP_TIMEOUT` | `10` | SMTP 超时（秒） |
| `SM_WORKSPACE_MAX_MEMBERS` | `0` | 工作区成员上限；`0` = 不限 |
| `SM_WORKSPACE_MAX_WATCHLIST_ITEMS` | `0` | 工作区自选股条数上限；`0` = 不限 |
| `SM_WORKSPACE_MAX_ALERT_RULES` | `0` | 工作区预警规则上限；`0` = 不限 |
| `SM_WORKSPACE_MAX_BACKTEST_RUNS` | `0` | 工作区回测记录上限；`0` = 不限 |
| `SM_WORKSPACE_MAX_STRATEGY_CONFIGS` | `0` | 选股/样本/算法配置各自的上限（按表分别计数）；`0` = 不限 |
| `SM_WORKSPACE_MAX_CHANNELS` | `0` | 工作区通知通道上限；`0` = 不限 |

配额变量在隔离关闭时一律不生效；超限返回 409 + `X-Auth-Error: quota_exceeded`。

`false` 时：workspace store 为空、请求不带 `X-Workspace-Id`、后端过滤退化为恒真、
`require_editor` 恒通过 —— 行为与引入工作区之前逐字节一致。

开启顺序建议：

1. 先升级代码（迁移自动执行，存量数据按用户回填个人工作区）；
2. 需要共享的数据用 `workspace claim` 移交到共享工作区；
3. 置 `SM_DATA_ISOLATION=true` 重启；
4. 页面右上角出现工作区切换器，owner 在「工作区」页邀请成员。

## 11. 已完成索引与已取消项

> 隔离相关的路线图项已全部落地；下面保留索引与「明确不做」的决策记录，避免后续重复讨论。

- ~~按工作区通知通道~~：**已实现** —— 见 §5.1（`0005_workspace_channels`、owner 管理
  webhook/Bark 通道、按工作区路由投递、`SM_ALERT_GLOBAL_FALLBACK` 兜底开关）；
- **配额与统计**：~~工作区级用量统计与可选配额、用户页数据概览、管理员用户统计列、
  管理员工作区总览页~~ **已实现** —— 见 §5.2（`GET /workspaces/{id}/stats`、
  6 个 `SM_WORKSPACE_MAX_*`、`WorkspaceOut.data_total`、管理员 `GET /users` 的 3 个
  统计字段、`/workspace` 页的「全部工作区」总览）；
- ~~工作区级预警参数覆盖~~：**已实现** —— 见 §5.3（`0006_workspace_alert_policies`、
  只收紧语义、owner 配置、引擎扫描与去重生效）；
- ~~共享清单可见性细分、回测结果分享链接~~：**已取消（2026-10-17）** —— 维持
  「同一工作区成员全量共享、角色只分读写」的语义，不引入清单级 private/shared 可见性；
  也不引入回测 `visibility`/`share_token`，回测结果只在所属工作区内可见（跨工作区 404
  的隔离语义不放宽）。`USER_ISOLATION.md` 中的对应待确认项仅保留为历史记录。
- ~~工作区级策略配置~~：**已实现** —— 选股 / 样本 / 算法三类命名配置随工作区隔离与角色权限，
  支持 `viewer` 只读运行；详见 [`docs/STRATEGY_CONFIGS.md`](STRATEGY_CONFIGS.md)。三张新表
  （`screening_configs` / `sample_configs` / `algorithm_configs`）已加入 `DATA_MODELS`：
  删除工作区时计入 `data_counts()` 与 `purge` 清理，`workspace claim` 也能认领
  `workspace_id = 0` 的未归属配置（同名冲突计入 `duplicates_skipped`）。

### P3 可选增强（已实现，2026-10-17，见 §5.4）

- 邮件通知通道（SMTP）与按工作区消息模板；
- 工作区用量 CSV 导出与每日快照趋势图。

### 后续可选（非隔离核心，按需评估）

- 短信通知（依赖具体服务商 SDK，当前未实现）；
- 备份异地留存（见 [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) §4）。
