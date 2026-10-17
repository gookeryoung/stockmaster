# 用户数据隔离设计

> **已被取代**：本文的 owner_id 用户级隔离已升级为 cndb 式工作区隔离
> （Workspace + owner/editor/viewer 角色），现行设计见 **`docs/WORKSPACE.md`**。
> 本文保留作为设计取舍的历史记录；`owner_id` 一律按现在的 `workspace_id` 理解。

> 状态：**已实施**（`0002_owner_columns` + 读写路径改造 + 交叉访问测试）；
> 其中归属列已在 `0003_workspaces` 改名并重新解释为 `workspace_id`。
> 开关：`SM_DATA_ISOLATION`，默认 `false`（= 隔离前的共享语义），可随时回退。
> 前置：账号体系（`backend/app/auth/`）已落地，`SM_AUTH_ENABLED` 控制是否强制登录。
> 本文记录设计取舍；实际落地位置见 §9。

---

## 1. 为什么要单独设计

账号体系解决的是「谁能进来」，隔离要解决的是「进来以后能看见谁的数据」。当前实现里，
只要开了 `SM_AUTH_ENABLED=true`，两个用户会互相看到并改写对方的自选股、预警规则和回测记录，
而且这不只是"越权"问题，是**功能性错误**：

| 现状 | 后果 |
|---|---|
| `uq_watchlist_item` = `(list_name, code)` | A 添加 `600519.SH` 到 `default` 后，B 再加同一只股票直接命中唯一约束（`add_watchlist_item` 会把它当成 A 的记录去改 `note`） |
| `AlertEngine._resolve_scope` 按 `list_name` 查自选 | 预警规则的 `scope_type=watchlist` 无法区分是谁的清单，A 的规则实际跑在 B 的自选上 |
| `GET /backtest/history` 无过滤 | 回测结果列表是全员共享的，且 `get_run` 用 uuid 直取，任何人拿到 id 就能读 |
| `_default_universe()` 取 `watchlist_items` 全表 | 每日同步/快照轮询把所有人的自选混成一个池子（这一点在多用户下其实是**期望行为**，见 §6） |

所以隔离不是加个 `WHERE` 就完事，需要同时处理：归属列的写入时机、唯一约束改造、
预警引擎在"无当前用户"的调度上下文里怎么解析归属、以及存量数据搬家。

---

## 2. 数据分三类，只有一类需要隔离

先划边界，避免把行情数据也按用户复制一份（那会让 800 只 × 5 年日线变成 N 份，彻底不可用）。

| 类别 | 表 | 归属 | 写权限 | 说明 |
|---|---|---|---|---|
| **A. 共享参考数据** | `instruments`、`trade_calendar`、`index_constituents`、`daily_bars`、`adjust_factors`、`indicators`、`factors`、`model_registry`、`predictions` | 无归属（全局一份） | 仅管理员/调度器 | 同样的输入对所有人生成同样的输出，复制没有意义 |
| **B. 共享运维数据** | `data_sync_tasks`、`data_sync_errors`、`auth_audit_logs`、`auth_flow_states` | 全局 | 调度器 | 同步任务描述的是"服务器干了什么"，不是"谁要求的" |
| **C. 用户私有数据** | `watchlist_items`、`alert_rules`、`alert_triggers`、`backtest_runs`(+`equity`/`trades`) | `owner_id` | 本人；管理员仅可读统计 | 真正的隔离对象 |

`backtest_equity` / `backtest_trades` 通过 `run_id` 外键级联，**不单独加 `owner_id`**：
归属由父行决定，加冗余列只会带来不一致（父行改了归属、子行没改）。

`alert_triggers` 是个例外——它的 owner 可以从 `rule_id` 推出，但**仍然加冗余 `owner_id`**：
触发记录是按 `trade_date` / `status` 高频查询的列表页，每次 join 回 `alert_rules` 才能过滤
会让索引失效；而且通知投递需要在不加载规则的情况下知道发给谁。写入时从 rule 复制，永不单独变更。

---

## 3. 归属列设计

统一命名 `owner_id`（而不是 `user_id`）：它将来可能指向"组/工作区"，语义上比 `user_id` 更耐改。

```python
# backend/app/db/models.py —— 四张表统一追加（OWNER_UNASSIGNED = 0）
owner_id: Mapped[int] = mapped_column(Integer, default=OWNER_UNASSIGNED, nullable=False)
```

约定：

- **`0` 是特殊值**，表示"未归属 / 历史数据 / 匿名会话"。不建对应的 `users` 行，
  避免外键约束把一个虚拟用户写进账号表。因此 `owner_id` **不加外键**。
- **一旦写入不可变**。不允许把记录转给别人（转移要走显式管理命令 + 审计），否则
  "谁创建的"就失去意义，也无法解释日志。
- 真实用户 id 从 1 开始（`users.id` 自增），与 `0` 永不冲突。

### 唯一约束改造

| 表 | 现在 | 改为 |
|---|---|---|
| `watchlist_items` | `(list_name, code)` | `(owner_id, list_name, code)` |
| `alert_rules` | 无 | 不加（同名规则允许，按 id 操作） |
| `alert_triggers` | `(rule_id, dedup_key)` | 不变（rule 已绑定单一 owner，天然不冲突） |

### 索引

```python
# 列表页的查询形态决定索引顺序，owner_id 必须打头
# watchlist 由唯一约束 (owner_id, list_name, code) 自带索引前缀，不另建
Index("ix_alert_rules_owner", "owner_id", "enabled")
Index("ix_alert_triggers_owner", "owner_id", "trigger_at")
Index("ix_backtest_runs_owner", "owner_id", "created_at")
```

原有的 `uq_watchlist_item` 与 `ix_triggers_date` / `ix_triggers_status` 保留或按实际查询删除，
不急于清理（SQLite 删索引便宜，删约束要重建表）。

---

## 4. 隔离怎么落实：三条路径，选"显式作用域 + 测试矩阵"

不采用数据库行级安全（SQLite 没有 RLS），也不建议全自动 `with_loader_criteria` 隐式过滤。
三个候选：

| 方案 | 做法 | 问题 |
|---|---|---|
| A. 每个查询手写 `where(owner_id == uid)` | 改动最小 | 漏一处就是越权，且评审时看不出来 |
| B. **显式作用域 + 统一入口**（推荐） | 私有用例一律经 `owned_stmt()` / `owned_or_404()`，归属从依赖注入拿 | 需要一次性的接口改造，但越权点收敛到两个函数 |
| C. SQLAlchemy `do_orm_execute` 全局自动加条件 | 写起来最省事 | 隐式作用域会波及调度器、管理员统计、CLI，排查"为什么查不到数据"极其痛苦 |

选 **B**，并用「交叉访问测试」兜底，而不是靠人肉 review 保证不漏：

```python
# backend/app/auth/deps.py —— 归属判定
def current_owner(user: User | None = Depends(current_user)) -> int:
    if not settings.data_isolation:
        return GUEST_OWNER_ID          # 开关关闭：全部落 0，等价旧语义
    return user.id if user is not None else GUEST_OWNER_ID
```

```python
# backend/app/db/scoped.py —— 统一入口（约 70 行）
def owned(stmt, model, owner):        # 给查询加 owner 条件；关闭时原样返回
    return stmt if not scope_enabled() else stmt.where(model.owner_id == owner)

def get_owned_or_404(session, model, pk, owner, label):
    """按主键取本人记录；不属于本人时返回 404 而不是 403。

    用 404 是为了不泄露「这个 id 存在但归别人」——否则可以拿自增 id 扫出
    全站有多少条规则、回测。
    """
```

要点：

- **写路径强制**：`create_*` 必须显式传 `owner_id=owner`，不依赖模型默认值 0（默认值只服务于迁移）。
- **读路径显式**：列表、详情、更新、删除四处全部走两个 helper。
- **管理员不自动越权**：`require_admin` 只用于"运维统计"和"用户管理"，
  不允许直接读写他人私有数据；确需排查时走单独的 `/admin/...` 端点并写审计。
- **匿名也可用**：`optional_user` 在没有 token 时不报错，返回 `None` → owner 0。
  这样 `SM_AUTH_ENABLED=false` 的单机行为与现在完全一致。

---

## 5. 存量数据迁移（Alembic）

本节原先建议在 `init_db()` 里挂一个自建的幂等 DDL 模块，**现已改为正式引入 Alembic**。
原因很实际：恢复旧备份要先把库放回旧 schema、再升级到当前版本，这件事只有版本链能做对；
自建模块只能覆盖"加列"这一种动作，遇到改唯一约束就得手写表重建脚本并自己保证幂等，
而「恢复 + 迁移」这条路上容错空间几乎为零。

已落地的基础设施（详见 `docs/IMPLEMENTATION_PLAN.md` §1）：

| 组件 | 作用 |
|---|---|
| `alembic.ini` + `backend/migrations/env.py` | 从 `settings.database_url` 取库；`render_as_batch=True`、`compare_type=True` |
| `0001_baseline`（空迁移） | schema 真源仍是 ORM 模型，baseline 只做「存量库可 stamp 的锚点」 |
| `app/db/migrations.py` | `ensure_migrated()` 处理三态：全新库 / 存量库（无版本表）/ 已纳管库 |
| `stockmaster db current\|history\|upgrade\|stamp\|check` | 运维入口，坏库也能跑（`main()` 对 `db`/`restore` 跳过 `init_db`） |

### 隔离的迁移：`0002_owner_columns`

1. **加列**：四张表 `ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0`。
   SQLite 允许「非空 + 常量默认值」的加列，不需要重建表。
2. **回填** `alert_triggers.owner_id` ← `alert_rules.owner_id`（同一条 SQL，可重入）。
3. **改 `watchlist_items` 唯一约束**：`(list_name, code)` → `(owner_id, list_name, code)`。
   SQLite 没有 `DROP CONSTRAINT`，只能重建表 —— 这正是必须用 Alembic 的地方：

   ```python
   with op.batch_alter_table("watchlist_items") as batch:
       batch.drop_constraint("uq_watchlist_item", type_="unique")
       batch.create_unique_constraint("uq_watchlist_item", ["owner_id", "list_name", "code"])
   ```

   `batch_alter_table` 在 `render_as_batch=True` 下会生成
   建新表 → `INSERT SELECT` → `DROP` → `RENAME` → 重建索引 的完整序列。
   手写这段时的坑：`engine.begin()` 事务内无法切换 `PRAGMA foreign_keys`；
   `watchlist_items` 没有被任何外键引用，所以本表不需要关外键。
4. **认领历史数据**：`stockmaster user claim --username boss`（先 `--dry-run` 看影响行数）。

### 灰度开关与顺序

`SM_DATA_ISOLATION=false|true`（默认 `false`，与现状等价）：

- `false`：查询不加 owner 过滤，但**写入照样记录 owner** —— 用来"先上线记录、再看数据、再开闸"；
- `true`：读写全部按 owner 过滤。

出问题只需改回环境变量，不必回滚数据库。

> 开 `SM_AUTH_ENABLED=true` 之前记得先 `user claim`：匿名访问被网关拦掉后，
> owner 0 的数据就没人看得见了。

## 6. 调度器与无用户上下文

后台任务没有"当前用户"，这是隔离最容易出事的地方（`AlertEngine.evaluate_all()` 在
`scheduler` 里跑，session 里没有 request）。

| 任务 | 处理方式 |
|---|---|
| 预警扫描 | 遍历规则（规则自带 owner），`scope_type=watchlist` 时按 `(rule.owner_id, list_name)` 解析自选。**这是必须改的一行**，否则规则仍然跑在别人的清单上 |
| 触发去重 | `dedup_key` 含 `rule.id`，天然按 owner 隔离，不用改 |
| 通知投递 | 按 `trigger.owner_id` 查该用户的通道配置；未配置则落全局 `SM_ALERT_WEBHOOK_URL`（现状即"全站一个 webhook"） |
| 每日同步 `_default_universe()` | 取**所有用户自选的并集**（`DISTINCT code`），语义是"任何人有兴趣的都要有数据"，比按 owner 分开同步省几十倍成本 |
| 快照轮询 `job_snapshot_poll` | 同上，并集后仍受 `[:200]` 上限；多用户超出时按"最近被预警规则引用"排序截断 |

原则：**共享参考数据只有一份，用户只决定"关注哪些"和"什么时候提醒我"**。

---

## 7. API 契约变化

| 端点 | 变化 |
|---|---|
| `GET /watchlist` | 只返回本人；`list_name` 语义从"全站清单名"变为"我的清单名" |
| `POST /watchlist` | 幂等键变为 `(本人, list_name, code)`；不再可能改到别人的 `note` |
| `DELETE /watchlist/{id}` | 非本人 → 404 |
| `GET/PATCH/DELETE /alerts/rules/{id}` | 非本人 → 404；列表只返回本人 |
| `GET /alerts/triggers` | 只返回本人规则的触发 |
| `PATCH /alerts/triggers/{id}` | 非本人 → 404 |
| `POST /alerts/scan` | 仅扫描本人规则（管理员可 `?scope=all`，需审计） |
| `POST /alerts/preview` | 预览用请求体里的 scope，不落库；`watchlist` 作用域按本人解析 |
| `GET /backtest/history` | 只返回本人 |
| `GET /backtest/{run_id}` | 非本人 → 404 |
| `GET /system/status` | `watchlist_size` 改为本人计数；新增管理员可见的 `owners` 汇总 |

兼容性：前端不需要改调用方式——它本来就只传 `list_name=default`，隔离后自动变成"我的 default"。
唯一需要动的是回测历史页（如果它做了"全站最近回测"的展示假设）和管理员用户页（可加"自选数/规则数"列）。

---

## 8. 管理员视角与配额

- **默认不跨用户读**：管理员能看"有多少用户、各自有多少条规则"，看不到规则内容。
  这是刻意的——私有数据的可解释性比运维便利更重要，且降低一次后台 XSS 的爆炸半径。
- 用户管理页新增只读统计列：自选数 / 规则数 / 回测数（`GROUP BY owner_id` 聚合，不进明细）。
- 可选配额（Phase 3，防滥用）：`SM_QUOTA_WATCHLIST`（默认 200）、`SM_QUOTA_ALERT_RULES`（默认 20）、
  `SM_QUOTA_BACKTESTS_PER_DAY`（默认 50）。超限返回 409 并给出当前用量，而不是静默截断。

---

## 9. 实施顺序（四个可独立上线的改动）

> 排期、验收口径与回滚方式统一维护在 `docs/IMPLEMENTATION_PLAN.md`；本节只说明「为什么这样切」。

1. ✅ **PR-1 归属列 + 迁移 + 写入记录**：`0002_owner_columns`、`user claim` 命令。
   隔离关时写路径照样记 owner，行为与升级前完全一致。
2. ✅ **PR-2 读路径隔离**：`current_owner` / `owned` / `get_owned_or_404` 接入
   watchlist + alerts + backtest，`AlertEngine._resolve_scope` 的 watchlist 分支按
   `(rule.owner_id, list_name)` 解析。
3. ⬜ **PR-3 按用户通知投递**：触发记录已冗余 `owner_id`，但 webhook 仍是全站一个
   （`SM_ALERT_WEBHOOK_URL`）；按用户配置通道需要新增表。
4. ⬜ **PR-4 管理员统计 + 配额**：用户页暂未显示各自的数据量，配额也未实施。

每一步都能单独回滚（PR-1 的列留着无害；PR-2 之后关掉开关即回到旧行为）。

## 10. 测试矩阵（这是"不漏"的真正保障）

已落地 `backend/tests/test_isolation.py`（9 项），用两个账号 A/B 对**每一个私有端点**做交叉访问，
参数化覆盖，新增端点漏加过滤时测试会红：

```python
PRIVATE_CASES = [
    ("GET",    "/api/v1/watchlist",              None),
    ("DELETE", "/api/v1/watchlist/{a_item}",     None),
    ("GET",    "/api/v1/alerts/rules",           None),
    ("PATCH",  "/api/v1/alerts/rules/{a_rule}",  {...}),
    ("DELETE", "/api/v1/alerts/rules/{a_rule}",  None),
    ("GET",    "/api/v1/alerts/triggers",        None),
    ("GET",    "/api/v1/backtest/{a_run}",       None),
]
# 断言：B 的 token 访问 A 的资源 → 404 或空列表，绝不返回 200 + A 的数据
```

另外必须覆盖的边界：

- 两个用户同名 `list_name=default` 各自添加同一只股票——双方都成功（旧约束会失败/串改）。
- 用户 B 的预警规则 `scope=watchlist` 只命中 B 的自选，**不是 A 的**（这条最容易回归）。
- 匿名（无 token）+ `SM_AUTH_ENABLED=false`：落到 owner 0，能读自己刚写的数据。
- 迁移幂等：对同一库连续跑两次 `upgrade()`，`user_version` 与行数不变。
- 删除用户：其私有数据一并删除（`ondelete` 走应用层显式删除，因为 `owner_id` 无外键），
  并断言不影响他人。

## 11. 待确认

1. **是否要"共享清单"概念**？例如团队公用一份 `default`，个人再加一层。当前设计是纯私有 +
   调度器取并集；若需要显式共享，`owner_id=0` 可以复用为"全员可见"（读时 `IN (me, 0)`），
   但写权限要单独定义。**默认按纯私有实施。**
2. **是否需要跨用户共享回测结果**（发链接给别人看）？若需要，`backtest_runs` 加
   `visibility`（private/link）+ `share_token`，而不是放宽 owner 过滤。
3. **迁移期存量数据归属**：默认保持 owner 0（匿名可见），开鉴权前用 `user claim` 交给管理员。
   若你希望自动认领给首个管理员，我可以在迁移里加这一步（有副作用，倾向显式命令）。
