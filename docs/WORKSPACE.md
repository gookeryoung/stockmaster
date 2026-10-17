# 工作区与角色权限设计

> 状态：**已实施**（`0003_workspaces` 迁移 + Workspace/WorkspaceMember 表 + 读写路径改造 + 角色矩阵测试）。
> 开关：`SM_DATA_ISOLATION`，默认 `false`（= 无工作区概念的共享语义），可随时回退。
> 前置：账号体系（`backend/app/auth/`）已落地。
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
# backend/app/workspace/roles.py
ROLE_RANK = {"owner": 3, "editor": 2, "viewer": 1}
GRANTABLE_ROLES = {"editor", "viewer"}      # owner 只能通过「转让」产生
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
# backend/app/db/models.py
WORKSPACE_UNASSIGNED = 0     # 原 OWNER_UNASSIGNED

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

### 共享参考数据不动

行情/指标/模型（类别 A）与同步任务/审计（类别 B）仍然全局一份。
**原则不变：共享参考数据只有一份，工作区只决定「关注哪些」和「什么时候提醒我」。**

## 4. 权限分层怎么落实

沿用「显式作用域 + 测试矩阵」方案，入口从 `auth/deps.current_owner` 换成
`workspace/deps`：

```python
# backend/app/workspace/deps.py
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

端点分层（`backend/app/api/v1/`）：

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
| 通知投递 | 仍是全站一个 webhook（`SM_ALERT_WEBHOOK_URL`），按工作区配通道是后续 PR |

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

## 9. 测试矩阵

- `backend/tests/test_workspace.py`：角色矩阵 —— viewer 写 403 + `X-Auth-Error: read_only`；
  editor 不可管成员；最后一个 owner 保护；跨工作区 404；转让后角色互换；
  preview 对 viewer 200；
- `backend/tests/test_isolation.py`：重写为工作区语义 —— 两个工作区对每个私有端点交叉访问，
  参数化断言「404 或空列表，绝不返回对方数据」；同名清单同码股票在两个工作区互不冲突；
  预警规则只命中本工作区自选；匿名 + 隔离关闭落 workspace 0。

## 10. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `SM_DATA_ISOLATION` | `false` | `false` = 无工作区概念，全员共享（旧语义，单机推荐）；`true` = 启用工作区隔离与角色权限 |

`false` 时：workspace store 为空、请求不带 `X-Workspace-Id`、后端过滤退化为恒真、
`require_editor` 恒通过 —— 行为与引入工作区之前逐字节一致。

开启顺序建议：

1. 先升级代码（迁移自动执行，存量数据按用户回填个人工作区）；
2. 需要共享的数据用 `workspace claim` 移交到共享工作区；
3. 置 `SM_DATA_ISOLATION=true` 重启；
4. 页面右上角出现工作区切换器，owner 在「工作区」页邀请成员。

## 11. 未做的（后续 PR）

- **按工作区通知通道**：webhook 仍是全站一个（原 PR-3）；
- **配额与统计**：工作区级配额、用户页数据量统计（原 PR-4）；
- 工作区级预警参数覆盖、共享清单细粒度可见性。
