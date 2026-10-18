# 自选等功能设计：清单管理 · 行情看板 · 选股入自选

> 状态：**设计稿，P0–P2 与增强已基本落地**：`/watchlist` 多清单管理、批量行情/评分、备注、
> 批量删除、CSV 导入导出（含目标清单选择）、选股结果入观察池、清单引用检查（预警引用阻止删除）、
> 统一确认弹层、自动刷新开关，以及后端 lists/rename/delete/batch-delete/import、
> `/market/quotes`、`/analysis/factors` 均已实现。未实现项：XLSX、拖拽排序与 sort_order、
> 撤销、清单引用检查的强一致服务端校验（见 §10）。
>
> 关联文档：
> - [界面设计总纲](UI_DESIGN.md) —— 设计原则、视觉 tokens、信息架构、线框图规范
> - [用户注册与登录设计](UI_AUTH.md) / [工作区 UI 设计](UI_WORKSPACE.md) —— 同一批设计稿
> - 现行后端设计：[工作区与角色权限设计](WORKSPACE.md)、[策略配置](STRATEGY_CONFIGS.md)、
>   [用户数据隔离设计（历史）](USER_ISOLATION.md)、[实施计划](IMPLEMENTATION_PLAN.md)
> - 代码索引：`src/stockmaster/plugins/system/routers/system.py`、
>   `src/stockmaster/plugins/market/routers/market.py`、`frontend/src/pages/Market.tsx`
>
> **范围**：把“自选”从行情页的一个按钮升级为完整工作流——自选清单管理页（`/watchlist`）、
> 自选行情看板、从自选发起预警/回测，以及把选股结果批量物化为观察池（自选清单）。
> 本文同时给出实现这些界面所缺的后端最小契约。
>
> **非目标（本设计明确不做）**：
> 1. 实时行情推送（WebSocket / SSE）与逐笔成交——本期行情以已存日线或低频快照为准；
> 2. 自选清单分组嵌套（清单 → 文件夹 → 清单）——清单是扁平命名空间；
> 3. 拖拽排序（无论清单顺序还是表内行顺序）作为 P0；持久化排序 `sort_order` 见 §10 未决问题；
> 4. 自选股基本面详情页、组合收益跟踪、多工作区之间复制清单；
> 5. 引入新的 UI 组件库、图表库或状态库（沿用总纲 §10.7）。

---

## 0. 阅读约定

本文沿用总纲的状态标记，不另造一套：

| 标记 | 含义 | 本文中的典型例子 |
|---|---|---|
| 【现状】 | 仓库已实现，可指到文件/端点 | `GET /watchlist`、`WatchlistItem` 唯一约束 |
| 【增量】 | 纯前端改造，不改后端契约 | 新增 `/watchlist` 页、Dashboard 换链接 |
| 【拟新增】 | 需要新后端接口/字段 | `GET /watchlist/lists`、`GET /market/quotes` |

补充约定：

- 所有端点均挂在 `/api/v1` 下，下文省略前缀；示例使用 `X-Workspace-Id: 3` 表示当前工作区。
- “清单”= `watchlist_items.list_name`；“条目/自选股”= 一行 `watchlist_items`；“观察池”= 由选股
  结果物化出来的清单，只是命名约定，**没有单独的实体**。
- 线框图图例沿用总纲 §8：`[按钮]`、`(○)/(●) 单选`、`[x]/[ ] 复选`、`▾ 下拉`、`⌕ 搜索`、
  `⋯ 更多`、`⚑ 预警`、`▶ 运行`、`* 必填`、`~ 截断`、`→ 跳转/流向`；桌面按 ≥1280px、移动按
  ≈390px 画，宽度 ≤100 字符。
- 金额用 `formatCompact`（万/亿），悬停显示元值；百分比保留 2 位；**红涨绿跌**（`--color-up`
  `#ef4444` / `--color-down` `#22c55e`），颜色不是唯一信息载体，涨跌必须同时带 `+/-` 号。

---

## 1. 目标与用户旅程

### 1.1 要解决的三类任务

| # | 用户任务 | 当前摩擦 | 设计后的关键路径 | 成功标准（可验收） |
|---|---|---|---|---|
| 1 | **盘后复盘看自选** | 只有行情页逐只搜代码；Dashboard 卡片只读；无法备注/排序/批量清理 | 总览 → `自选` → 切清单 → 按涨跌幅排序 → 看 K 线 → 写备注 | 从总览进入“看到某只自选的最新价与涨跌幅”≤ 2 次点击；清单内备注可原地改 |
| 2 | **把选股结果变成观察池** | 分析页/策略页运行结果只能看，代码要手工抄 | 运行选股 → 勾选/全部 → 弹层选清单（可新建）→ 反馈 added/skipped → 跳自选页 | 从“选股运行完成”到“观察池里出现全部命中标的”≤ 3 次点击；重复加入不产生重复行 |
| 3 | **从自选直接挂预警/回测** | 需要去预警页手填代码、去回测页手填代码 | 自选行 `⋯` → 加预警（预填范围与代码）/ 去回测（预填标的） | 预填后用户只需确认参数；无预填时平均需要 4 个以上字段输入 |

关联指标（研究工具不追求点击率，只看路径是否可达）：自选页周活跃工作区数、从自选发起的预警规则数、
选股结果“加入自选”使用次数、导入失败行占比（应 < 5%）。

### 1.2 旅程 A：盘后复盘看自选

```text
19:30  总览「自选卡片」 → 自选页 /watchlist → 切清单「核心组合」
                      → 按涨跌幅降序（红涨绿跌 + 正负号） → 点代码看 K 线
                      → 备注内联编辑 → 行内 ⋯ → 加预警 / 去回测
```

关键点：首屏默认显示“最近一次复盘看的清单”（本地记住），不要求每次重选；行情列失败不阻塞备注与
清单操作（分区降级，见 §7）。

### 1.3 旅程 B：选股结果 → 观察池

```text
分析页选股 / 策略页「选股运行」结果
  → 勾选 N 只（或「全部加入自选」） → 弹层选目标清单（可现场新建）
  → POST /watchlist/import【拟新增】 → 反馈「新增 3 · 跳过 1 · 非法 0」
  → [查看观察池 →] 跳自选页并选中该清单
```

关键点：清单名允许现场新建，且“新建的空清单”本身不落库（见 §4.2）；重复加入必须幂等，反馈要区分
`added / updated / skipped`。

### 1.4 旅程 C：从自选直接挂预警/回测

```text
自选表某行「⋯」
  ├─ ⚑ 加预警 → 预警页（预填）：仅该股 scope=codes + code；整清单 scope=watchlist + list_name
  │             规则名「自选预警 · 贵州茅台(600519.SH)」，阈值用最新价预填，[创建规则]
  └─ ▶ 去回测 → 回测页（预填 codes=<code>，策略与区间不变）；「回测整个清单」≤50 只
```

关键点：预警的“范围”与“代码”是**二选一**的现有后端语义——`scope_type=watchlist` 只能指定清单名，
不能在同一规则里再限定单只股票（`src/stockmaster/schemas/__init__.py:439-449`）。因此“一键加预警”
必须让用户在“只盯这一只（codes）”和“盯整个清单（watchlist）”之间选，详见 §4.4。

---

## 2. 现有基线【现状】

### 2.1 页面与入口盘点

| 位置 | 文件 | 现有能力 | 局限 |
|---|---|---|---|
| 行情页 `/market` | `frontend/src/pages/Market.tsx:30-35,67-76` | 查询当前工作区 `default` 清单；选中代码后一个「加入自选」按钮，已在自选则禁用 | 只能加入 `default`；不能选清单/写备注；加入后无“查看自选”入口 |
| 总览页 `/` | `frontend/src/pages/Dashboard.tsx:16,99-122` | 只读卡片显示 `default` 清单前 8 只的代码/名称 | 无行情、无涨跌幅、无备注；“管理”链到 `/market` |
| 设置页 `/settings` | `frontend/src/pages/Settings.tsx:40-66,124-177` | 输入代码加入 `default`；列表可逐条“移除” | 只是运维式管理，不是研究动线；无多清单 |
| 独立自选页 | — | **不存在** | 本次新增 `/watchlist`【增量】 |
| 分析页选股结果 | `frontend/src/pages/Analyze.tsx:197-239` | 表格展示 `ScreenRow`（评分/动量/波动/换手/RSI） | 无“加入自选”动作；结果不持久化，刷新即丢 |
| 策略页选股运行结果 | `frontend/src/pages/StrategyConfigs.tsx:473-525` | 运行后展示 `ScreeningRunResult.items` | 同上；且结果只在组件 state，切走 tab 即丢 |
| 预警页 | `frontend/src/pages/Alerts.tsx:158-184` | `scope_type` 下拉 + 自由文本 `scope_value` | 只能手填清单名/代码，不能从自选带入 |
| 回测页 | `frontend/src/pages/Backtest.tsx:21-50,141` | 代码文本框（逗号分隔）+ 策略/区间 | 不能从自选带入代码 |

当前前端**没有**任何“枚举清单、清单计数、批量导入、批量删除、批量行情、批量评分”的调用：
`frontend/src/api.ts:467-475` 只有 `watchlist(listName)`、`addWatch(...)`、`removeWatch(id)` 三个函数。

### 2.2 现有后端 watchlist 契约

代码位置：`src/stockmaster/plugins/system/routers/system.py:51-140`，请求/响应模型在
`src/stockmaster/schemas/__init__.py:489-502`。

| 方法/路径 | 依赖 | 成功 | 主要错误 | 行为细节 |
|---|---|---|---|---|
| `GET /watchlist?list_name=default` | `current_workspace` | `200`，`WatchlistItemOut[]` | — | 只取**一个**清单；按 `code` 升序；名称来自 `instruments` 表，查不到时 `name=""`；未知清单返回 `[]`，与“空清单”无法区分 |
| `POST /watchlist` | `require_editor` | `201`（**更新已有条目也返回 201**） | `422` 代码非法；`409` + `X-Auth-Error: quota_exceeded`；`403` + `X-Auth-Error: read_only` | 先 `normalize(code)`；按 `(workspace_id,list_name,code)` 查重；存在则**只更新 note**，不存在才占配额并插入；返回的 `name` 为代码对应名称 |
| `DELETE /watchlist/{item_id}` | `require_editor` | `204` 无 body | `404`（不存在或属于别的工作区）；`403 read_only` | 按**条目 id** 删除；没有批量；跨工作区与不存在不可区分（刻意，避免枚举） |

`GET` 响应示例【现状】：

```json
[
  { "id": 12, "list_name": "default", "code": "600519.SH", "name": "贵州茅台", "note": "" }
]
```

`POST` 请求/响应示例【现状】（幂等语义）：

```json
// 请求
{ "code": "600519", "list_name": "default", "note": "等待回调" }
// 响应 201（无论新建还是更新已有条目）
{ "id": 12, "list_name": "default", "code": "600519.SH", "name": "贵州茅台", "note": "等待回调" }
```

> 注意两个现状细节，前端与契约设计都必须适配：
> 1. `POST` 对 `note` 是**整体替换**：不传 `note` 等价于传 `""`，会把已有备注清空；
> 2. 更新已有条目也返回 `201`，前端不能靠状态码判断“新增还是更新”，只能靠是否已在当前清单中
>    预先判断（这也是批量 import 要返回 `added/updated` 的原因）。

### 2.3 数据模型与唯一约束【现状】

`src/stockmaster/db/models.py:437-452`：

| 字段 | 类型 | 约束/默认 | 说明 |
|---|---|---|---|
| `id` | Integer | 主键自增 | 前端删除/批量删除使用 |
| `workspace_id` | Integer | 非空，默认 `WORKSPACE_UNASSIGNED=0` | 隔离关闭时全部落 0 号共享桶 |
| `list_name` | String(64) | 默认 `"default"` | 清单名；**API schema 未做长度校验**（见 §2.5/§5.2.2） |
| `code` | String(16) | 非空 | 规范形式 `600519.SH`（`core/symbols.py::normalize`） |
| `note` | String(256) | 默认 `""` | 备注；同样只由数据库列长度约束 |
| `added_at` | DateTime | 默认 UTC now | 现有界面未展示；本文用于排序/清单“最近添加” |
| 唯一约束 | — | `UniqueConstraint("workspace_id","list_name","code", name="uq_watchlist_item")` | 同一工作区同一清单同一代码只有一行；不同工作区互不影响 |

隔离语义【现状】：`core/workspace_deps.py:87-117` 决定 `workspace_id`；`db/scoped.py::in_workspace`
在 `SM_DATA_ISOLATION=false` 时退化为恒真（全员共享 0 号桶），`true` 时按工作区过滤。
配额【现状】：`core/quota.py:126-147`，资源键 `watchlist_items`（`USAGE_COUNTERS`），
`SM_WORKSPACE_MAX_WATCHLIST_ITEMS` 默认 `0`=不限；仅隔离开启且 `workspace_id > 0` 时生效；
**幂等更新已有条目不占配额**（`system.py:114` 注释与分支）。

### 2.4 可复用的行情、选股、回测与预警能力【现状】

| 能力 | 端点 | 现状要点 | 对本设计的可复用性 |
|---|---|---|---|
| 证券主数据 | `GET /stocks?q=&limit=` | 返回 `InstrumentPage`（代码/名称/板块/ST/状态） | 搜索加自选、名称补齐 |
| 日线 | `GET /stocks/{code}/bars?adjust=qfq&limit=` | 404 时带 `X-StockMaster-Reason: data-not-synced` 与可用复权序列 | P0 行情列兜底（取最后两根 bar） |
| 指标 | `GET /stocks/{code}/indicators` | 历史 <30 根返回 409 | 自选页暂不展示，查看 K 线复用 |
| 快照 | `GET /stocks/{code}/snapshot` | 只用 provider 链，单只；`503` 表示取不到；返回体**不含换手率**（`market.py:251-281`） | 仅作 P2 批量行情的底层能力，不建议逐只调用 |
| 因子（单只） | `GET /analysis/factors/{code}` | 读 `factors` 表最新一行；无数据 404 | 评分列 P1 可逐只取（N 次往返），P2 改批量 |
| 选股 | `POST /analysis/screen` | `ScreenRow[]`；`limit` 1..500；默认最近因子日；`close` 恒为 null | 选股结果入自选的输入源 |
| 策略选股 | `POST /strategy/screenings/{id}/run` | `ScreeningRunResult{trade_date,count,items}`；viewer 可运行（只读求值） | 选股结果入自选的输入源 |
| 回测 | `POST /backtest/run` | `require_editor`；先查配额；`codes` 优先、`screening_id` 次之；无标的/无信号 409 | “去回测”直接复用，不新增端点 |
| 预警类型 | `GET /alerts/types` | 返回规则类型与参数 schema | 加预警时动态生成参数表单 |
| 预警预览 | `POST /alerts/preview` | 只读求值；viewer 可用；`watchlist` scope 按工作区解析 | 加预警弹层可“先预览命中” |
| 预警创建 | `POST /alerts/rules` | `require_editor`；参数校验 422；配额 409 | 复用，不新增端点 |
| 系统状态 | `GET /system/status` | `data.watchlist_size`、`data.latest_bar_date`、数据源熔断状态 | 数据日期/stale 提示、配额粗略提示 |
| 工作区用量 | `GET /workspaces/{id}/stats` | `usage` + `limits` + `labels`；成员可读 | 配额精确提示（仅工作区启用时） |
| 行情概览 | `GET /market/overview` | `as_of`（最新 qfq 日线日期）、宽度、指数 | 全市场数据日期参考 |

### 2.5 现状限制清单（本文要解决/绕开的）

1. **无法枚举清单**：`GET /watchlist` 必须显式传 `list_name`，前端不知道工作区还有哪些清单与计数，
   左栏无法实现（P1 新增 `GET /watchlist/lists`）。
2. **只能单条删除/无清单级操作**：`DELETE /watchlist/{id}` 一次一条；清单无法重命名/整单删除；批量删除需 N 次请求且部分失败不可汇总（P1 新增批次与清单端点）。
3. **没有批量新增**：选股结果入自选只能逐条 `POST`，50 只 = 50 次往返，配额分散检查，中途超限会留下“加了一半”（P1 新增 `POST /watchlist/import`）。
4. **没有批量行情/评分**：逐只 `/snapshot` 每次触发一次全市场快照拉取（§5.2.6）；评分只有单只端点，
   N 次 HTTP 在 50 只时明显（P2 新增 `/market/quotes` 与批量因子；P0/P1 评分列可隐藏）。
5. **排序不持久化**：有 `added_at` 但无 `sort_order`，手工排序无法保存（§10 未决问题）。
6. **长度只由数据库约束**：`WatchlistItemIn` 对 `list_name`/`note` 无 `max_length`，SQLite 不强制
   `VARCHAR` 长度；超长值可能入库（拟新增端点必须先校验）。
7. **前端拿不到错误头**：`ApiError` 只保留 `status/detail`，丢弃 `X-Auth-Error`
   （`api.ts:53-63,145-163`），只能靠文案判断（建议补 `code`）。
8. **Dashboard 入口指向错误**：自选卡片“管理”链到 `/market`（`Dashboard.tsx:102`），本文改为
   `/watchlist`【增量】。

---

## 3. 信息架构与导航【增量】

### 3.1 目标导航（与总纲 §4.2 一致）

```text
总览  行情  自选  分析  策略  预测  回测  预警  设置  [工作区]  [用户]
              ↑ 新增一级入口，位于「行情」之后
```

| 项 | 现状 | 目标【增量】 |
|---|---|---|
| 一级导航 | `App.tsx:81-92`：总览/行情/分析/策略/预测/回测/预警/设置 | 在“行情”与“分析”之间插入 `{ to: "/watchlist", label: "自选" }` |
| 路由 | `App.tsx:137-150` 无 `/watchlist` | 新增 `<Route path="/watchlist" element={<Watchlist />} />` |
| 总览自选卡片 | “管理”→`/market` | “管理”→`/watchlist`；卡片本身升级见 §4.9 |
| 行情页加自选 | 只加入 `default` | 保留快捷按钮；点击后可选清单（P1），默认 `default`；无清单时可直接新建 |
| 分析页选股结果 | 无动作 | 行级 `+ 加入自选` + 工具栏 `全部加入自选`（§4.8） |
| 策略页选股运行结果 | 无动作 | 同上（§4.8） |
| 设置页自选区 | 添加/移除默认清单 | 保留兼容入口，增加“前往自选页管理 →”，并显示当前清单名（避免用户以为只有一份清单） |

导航规则沿用总纲：登录前不渲染导航；移动端导航横向滚动；工作区切换后整棵路由子树重挂载，
自选页的所有查询自动重取（`App.tsx:136-150` 的 `key={currentId ?? "local"}`）。

### 3.2 页面与路由表

| 路由 | 页面 | 文件 | 访问条件 | 本期动作 |
|---|---|---|---|---|
| `/watchlist` | 自选清单 + 行情看板 | `frontend/src/pages/Watchlist.tsx`（**新文件**） | 门禁通过（`SessionGate`） | 新增 |
| `/market` | 行情 K 线 | `Market.tsx` | 门禁通过 | 加自选支持选清单 |
| `/analyze` | 分析 | `Analyze.tsx` | 门禁通过 | 选股结果加入自选 |
| `/strategy` | 策略 | `StrategyConfigs.tsx` | 门禁通过 | 选股运行结果加入自选 |
| `/alerts` | 预警 | `Alerts.tsx` | 门禁通过 | 接收“自选预填”并一次消费 |
| `/backtest` | 回测 | `Backtest.tsx` | 门禁通过 | 接收“自选预填代码”（view store 已具备） |
| `/` | 总览 | `Dashboard.tsx` | 门禁通过 | 自选卡片升级 + 跳转修正 |

不新增弹层路由：清单选择、导入、批量确认都走组件内 modal（总纲 §10.4）。

### 3.3 前端状态归属

沿用总纲“操作状态进 view store、服务端数据进 React Query”的边界（`frontend/src/view.ts:1-14`）。

| 状态 | 归属 | 持久化 | 说明 |
|---|---|---|---|
| 当前清单 `listName` | `view.ts` 新增 `watchlist` 切片 | 是（localStorage） | 默认 `"default"`；切工作区后仍保留名字（若不存在，回退 default 并提示） |
| 已知清单名 `knownLists` | 同上 | 是 | P0 无 `lists` 端点时的降级枚举；P1 起仅作缓存，服务端返回为准 |
| 表头排序 `sort` | 同上 | 是 | `{field, direction}`，见 §4.3 |
| 搜索词 `search` | 同上 | 否/可选 | 刷新后清空更符合“重新找股票”的预期 |
| 选中行 `selectedIds` | 页面组件 `useState` | 否 | 切清单、改搜索时清空，避免误删不可见行 |
| 备注编辑草稿 | 单元格组件 `useState` | 否 | 失焦/Enter 提交，Esc 取消 |
| 服务端数据 | React Query | 缓存 | key 见下表 |
| “预填预警”一次性信息 | `view.ts` 非持久化字段（`partialize` 排除） | 否 | Alerts 页挂载时消费并清空，避免下次进入还带着旧代码 |

查询 key 统一带工作区维度：`["watchlist","lists",workspaceId]`、`["watchlist","items",workspaceId,listName]`、
`["watchlist","quotes",workspaceId,codes.join(",")]`、`["watchlist","scores",workspaceId,...]`；写操作后
按前缀 `["watchlist"]` 失效即可覆盖旧页面的无维度 key（`Dashboard/Market/Settings` 现状为 `["watchlist"]`）。

---

## 4. 页面设计

### 4.1 自选页整体布局

桌面（≥ 1280px，左侧清单栏 240–280px + 右侧行情表）：

```text
┌ 自选 ───────────────────────────────────────────────────────────────────────┐
│ 清单                │ 核心组合 · 12 只        [＋ 加自选] [导入] [导出] [⋯]  │
│ ● default      8    │ ⌕ 代码/名称/备注   排序: 代码 ▾   数据 2026-10-17 收盘 │
│ ● 核心组合    12    │ ┌───────────────────────────────────────────────────────┐│
│ ● 观察池-1017  6    │ │[ ] 代码      名称   最新价  涨跌幅  成交额 换手 操作 ││
│                     │ │[ ] 600519.SH 茅台  1680.00 +1.20%  12.3亿 0.35 ⋯  ││
│ [＋ 新建清单]       │ │[ ] 000001.SZ 平安    11.20 -0.88%   8.1亿 0.61 ⋯  ││
│ 清单条目共 26 只    │ └───────────────────────────────────────────────────────┘│
│ 行情不可用时仍可     │ 已选 2 项  [批量删除]       备注可点击原地编辑           │
│ 管理清单（降级提示） │                                                       │
└─────────────────────┴───────────────────────────────────────────────────────┘
```

移动（≈ 390px）：

```text
┌ 自选 ────────────────────────────────┐
│ [核心组合 ▾]      [＋] [导入] [⋯]    │
│ 12 只 · 数据 2026-10-17 收盘         │
│ ┌──────────────────────────────────┐ │
│ │[ ] 代码    名称   最新价  涨跌幅 │ │
│ │[ ] 600519 茅台  1680.00 +1.20%   │ │
│ │[ ] 000001 平安    11.20 -0.88%   │ │
│ │ ← 横向滑动查看 成交额/换手/操作   │ │
│ └──────────────────────────────────┘ │
│ 已选 2 项  [批量删除]                │
└──────────────────────────────────────┘
```

布局规格：

| 区域 | 规格 |
|---|---|
| 页面容器 | `space-y-4`；两个 `.card` 组成 `grid grid-cols-1 xl:grid-cols-[260px_1fr] gap-4` |
| 左清单栏 | `.card p-0`；清单行 44px 高、可点击整行切换；当前清单 `bg-sky-950/40 border-l-2 border-sky-500`；计数右对齐 `text-slate-500`；清单多时内部滚动 `max-h-[520px] overflow-y-auto` |
| 右主区 | `.card`：工具栏（`flex flex-wrap items-center gap-2`）+ `.table-wrap` 表格 + 选中工具条 |
| 移动端 | 左栏折叠进工具栏下拉 `[当前清单 ▾]`；`<640px` 隐藏次要列（备注列并入操作菜单），表格 `.table-wrap` 横向滚动 |
| 页面标题 | 不单独做 hero；左侧栏顶部 `text-sm font-medium text-slate-300` “清单”，右侧工具栏显示当前清单名 + 计数 |

交互：

- 切换清单：写 `view.ts.watchlist.listName`；清空选择；触发新查询；滚动回顶部；接口 404/空时显示空态
  （§4.2），不跳走；首屏左栏与右表各自骨架，互不阻塞。
- 清单栏底部显示“清单条目合计”（`lists` 求和，P1）；P0 仅显示当前清单计数。

### 4.2 清单管理

#### 4.2.1 清单的生命周期（关键设计决策）

清单**不是独立实体**，只是 `watchlist_items.list_name` 的去重值。由此推出：

| 场景 | 行为 | 理由 |
|---|---|---|
| 新建空清单 | 先在 UI 记一个“草稿清单”（`knownLists` + 当前选中），**不落库**；用户加入第一只股票/导入后清单才真正出现在 `GET /watchlist/lists` | 无 `watchlist_lists` 表；不新增迁移即可实现多清单 |
| 新建清单后放弃 | 无任何行 → 自动消失；前端提示“空清单不会保存，加入第一只股票后生效” | 避免“空清单”与“不存在”两套语义 |
| 重命名清单 | 服务端对 `list_name` 做一次批量 UPDATE（P1 端点） | 保持无新表；引用旧名的预警/样本需提示 |
| 删除清单 | 删除该清单全部行（P1 端点），条目备注一起删除；行情/预警/回测历史不受影响 | 删除影响面必须写清 |
| `default` | 与普通清单同等对待（可改名/删除），但作为缺省值：加入自选未指定清单时使用 | 兼容现有 `Market/Settings/Dashboard` 的硬编码默认 |
| 清单名重复 | 同一工作区内重名 = 同一清单（合并），服务端不报错；重命名到已存在名字 → 409 | 与唯一约束 `(workspace_id,list_name,code)` 一致 |

> 为什么重命名/删除要看“引用”：`AlertRule.scope_type=watchlist` 的 `scope_value` 是清单名；
> `SampleConfig.universe_type=watchlist` 的 `universe_value` 也是清单名。重命名不会自动改这些引用。
> 更危险的是删除：`AlertEngine._resolve_scope` 在清单为空时会**回退为全市场扫描**
> （`src/stockmaster/plugins/alerts/services/engine.py:309-324`）。因此删除/改名对话框必须提示引用数，
> 并默认阻止“删除仍被预警引用的清单”。

#### 4.2.2 线框图：清单栏

```text
┌ 清单 ─────────────────────────┐
│ ● default             8      │  ← 当前清单：左侧竖条 + 高亮
│ ● 核心组合           12      │
│ ● 观察池-1017         6      │
│ ● 待验证              0      │  ← 草稿清单（本地，未落库）
│ [＋ 新建清单]                 │
└───────────────────────────────┘
```

新建/重命名用同一种表单弹层（名称 *、字数上限 64、影响面说明），删除用危险确认（列出条数 + 引用数 +
“清单为空时引用它的预警规则会退化为全市场扫描”的警告 + `[ ] 我已了解` 复选）。

清单操作规格：

| 操作 | 入口 | 服务端 | 前端交互 | 错误处理 |
|---|---|---|---|---|
| 切换清单 | 点清单行 | `GET /watchlist?list_name=` | 立即切换 + 查询；保留排序；清空选择 | 失败保留旧清单并提示重试 |
| 新建清单 | 栏底按钮 | 无（草稿）→ 首次加入时 `POST`/`import` | 弹层输入；创建后自动打开“加自选”搜索框 | 重名直接切到已有清单并提示 |
| 重命名 | 清单行 `⋯` → 重命名 | `PATCH /watchlist/lists/{name}`【拟新增】 | 弹层；成功后 invalidate lists/items；`view.listName` 同步新名 | 404（清单已消失）→ 刷新列表；409（重名）→ 字段级错误 |
| 删除 | 清单行 `⋯` → 删除 | `DELETE /watchlist/lists/{name}`【拟新增】 | 危险确认（引用检查见上）；成功后选下一个清单 | 404 → 视为已删除；403 → 只读提示 |
| 查看计数 | 清单行右侧 | 随 `lists` 返回 | 计数 = 清单条目数；0 只显示“0” | — |

文案：

- 空清单（有清单但 0 条）：“「核心组合」还没有股票。 [去行情页添加] [导入 CSV]”
- 无任何清单（新工作区）：“还没有自选清单。自选清单用来把关注的股票按策略分组。[去行情页挑几只 →] [新建清单]”
- 清单区接口失败：“清单加载失败：<原因> [重试]”；行情区仍可显示旧的当前清单缓存（若有）。

### 4.3 行情表格：列、排序、数据日期

#### 4.3.1 列定义

| # | 列 | 最小宽度 | 内容/格式 | 数据来源 | 可排序 | 空值 |
|---|---|---|---|---|---|---|
| 0 | 复选 | 36px | `[ ]`/`[x]`，表头三态全选 | 前端 | 否 | — |
| 1 | 代码 | 104px | `600519.SH`，等宽字体；点击 = 查看 K 线（按钮语义） | `WatchlistItemOut.code` | 是（默认升序） | — |
| 2 | 名称 | 120px | 中文名称；`title` 显示代码 | `WatchlistItemOut.name`（空则 `--`） | 否（按名称搜索） | `--` |
| 3 | 最新价 | 92px | `1680.00`；右对齐；红/绿 + 平盘 | P0：`bars[-1].close`；P2：`quotes.price` | 是 | `--` |
| 4 | 涨跌幅 | 100px | `+1.20%`（含符号）；红/绿 | P0：`bars[-1].pct_chg` 或 `close/preclose-1`；P2：`quotes.pct_chg` | 是 | `--` |
| 5 | 成交额 | 100px | `12.30亿`；右对齐；`title` 显示元值 | `bars[-1].amount` / `quotes.amount` | 是 | `--` |
| 6 | 换手率 | 88px | `0.35%`；右对齐 | `bars[-1].turn`；P2 `quotes.turnover` | 是 | `--` |
| 7 | 评分 | 84px | `72.4`，保留 1–2 位；`title` 显示因子日 | P1 单只 factors（可选）；P2 批量 factors | 是（P2 起） | `--` |
| 8 | 备注 | 160px | 截断 `~`，点击进入行内编辑；空显示“添加备注”灰字 | `WatchlistItemOut.note` | 否 | 空 |
| 9 | 操作 | 120px | `⚑ 预警` `▶ 回测` `K线` `⋯`（含“移出自选”） | 前端 | 否 | — |

说明：

- P0 不展示“评分”列（无批量接口，逐只取会造成 N 次请求）；P1 可逐只取并隐藏/显示开关，P2 起默认展示。
- 快照导出体没有换手字段（`market.py:268-280`），P0 用日线 `turn`（“最新交易日换手”，tooltip 注明
  “按日线”）；P2 批量行情补 `turnover`。名称缺失时显示代码 + 灰色“未同步名称”。

#### 4.3.2 排序规则

| 规则 | 规格 |
|---|---|
| 默认 | 代码升序（与 `GET /watchlist` 的 `ORDER BY code` 一致）；首次进入无需额外点击 |
| 交互 | 点击可排序表头：第 1 次升序、第 2 次降序、第 3 次回到默认；表头显示 `▲/▼`；`th` 加 `aria-sort`（`ascending`/`descending`/`none`） |
| 排序实现 | 前端对已加载的当前清单全量排序（无分页，清单规模受配额约束）；不对服务端排序提要求 |
| 空值 | `null` 永远排最后（无论升/降序），与 `screening.py` 的排序语义一致 |
| 涨跌幅排序 | 按数值而非字符串；`+0.5%` 与 `-3%` 顺序正确 |
| 持久化 | `view.ts.watchlist.sort`；刷新后保留；工作区/清单切换不重置排序（用户预期一致） |
| 多级排序 | 不做（P2 之后视需求） |

#### 4.3.3 表头、数据日期与 stale

表格上方工具栏右侧：

```text
数据 2026-10-17 收盘 · 行情快照 14:55  [刷新]
```

| 数据态 | 判定 | 展示 | 后续动作 |
|---|---|---|---|
| 最新 | 个股行情日期 = 全市场最新日线日期（`/system/status.data.latest_bar_date` 或 `quotes.as_of`） | `数据 2026-10-17 收盘`（`text-slate-500`） | — |
| stale | 个股日期落后于全市场最新日期 1 个交易日以上 | 行内最新价旁 `badge bg-amber-900/50 text-amber-300` “旧数据”；tooltip “该股日线最后日期 2026-10-15” | 提示“去设置页同步该股日线”；批量同步入口复用设置页任务 |
| 未同步 | `bars` 404 且 `X-StockMaster-Reason: data-not-synced` | 价格/涨跌幅/成交额/换手全部 `--`，行内灰色“未同步” | 行内 `⋯` → “去同步”（跳 `/settings`，预填代码§P2）；不把缺失当 0 |
| 行情不可用 | `quotes` 503 / 网络失败 | 顶部 amber 横幅“行情暂不可用：<原因> [重试]”；清单与备注操作仍可用 | 重试；查看状态栏供应商熔断提示 |
| 盘中/收盘 | `quotes.source=snapshot` 且 `quote_time` 非空 | 显示 `行情快照 14:55`；否则 `数据 <date> 收盘` | 切换“实时快照”开关（P2 可选，见 §10） |

加载态：首次加载显示 5–8 行骨架（`animate-pulse` 的 `slate-800` 条），表头保持可见；刷新时在工具栏
显示“刷新中…”，不清空旧数据（避免闪烁）。

搜索：工具栏 `⌕` 输入按代码/名称/备注前端过滤（大小写不敏感，代码支持去交易所后缀匹配）；
搜索无结果：“没有匹配「xxx」的自选 [清除搜索]”。

### 4.4 行内操作：加预警、去回测、看 K 线、移出自选

行尾操作按宽度分级：桌面显示 `⚑ 预警`、`▶ 回测`、`K线`，其余进 `⋯`；移动端全部进 `⋯`。

| 动作 | 触发 | 预填/跳转 | 复用端点【现状】 | 只读(viewer) |
|---|---|---|---|---|
| **加预警（单只）** | `⚑ 预警` → 菜单默认项 | 预警页；`scope_type=codes`、`scope_value=<code>`、`name=自选预警 · <name>(<code>)`；规则类型默认 `price_cross`，阈值预填当前最新价（若可得） | `GET /alerts/types`、`POST /alerts/preview`、`POST /alerts/rules` | 菜单项禁用 + `title="查看者无法创建预警规则"` |
| **加预警（整个清单）** | `⚑ 预警` → “监听本清单” | 预警页；`scope_type=watchlist`、`scope_value=<listName>`；名称 `自选清单 · <listName>` | 同上 | 禁用并说明 |
| **去回测** | `▶ 回测` | 回测页；`codes=<code>`；保留用户上次的策略/区间/成本设置 | `POST /backtest/run` | 禁用 + 说明“回测会消耗计算资源，需要编辑者权限” |
| **回测整个清单** | `⋯` → 回测整个清单（P2） | 回测页；`codes=<清单内代码逗号拼接>`，去重、≤50 只；超过 50 只时提示“最多回测 50 只，建议先用选股条件缩减” | 同上 | 禁用 |
| **查看 K 线** | 点代码单元格 / `K线` | 行情页；`view.market.selected=<code>`；`adjust` 保持用户上次选择 | `GET /stocks/{code}/bars`、`/indicators` | 可用（只读） |
| **移出自选** | `⋯` → 移出自选 | 直接 `DELETE /watchlist/{id}`；成功后 toast + 5 秒“撤销”（P1：撤销 = 重新 `POST` 同 code/note） | `DELETE /watchlist/{id}` | 禁用 |

“加预警”不能预填成“清单 + 单只代码”的原因（必须写进实现注释）：`AlertRuleIn.scope_type` 是四选一
（`all/index/watchlist/codes`），`watchlist` 的 `scope_value` 只表示清单名，规则内部没有“再按 code
过滤”的字段。把单只股票塞进 `watchlist` scope 会被解析成整个清单，属于静默放大范围。故拆成两个
显式选项。若产品坚持“点一下就直接进单只预警编辑器”，则默认走 `codes`。

预警预填的实现约定：

1. 自选页写 `view.ts.alerts.form`（scope/name 等）与 `alerts.params`（类型默认参数字符串）；
   阈值 = 当前价时写 `params.threshold=String(price)`；同时设置一次性 `alertPrefill`；
2. `navigate("/alerts")`；Alerts 页 `useEffect` 首次挂载时若存在 `alertPrefill`，展示蓝色提示条
   “已从自选带入：600519.SH / 核心组合 [清除]”，消费后清空；
3. 用户修改任意字段即视为“已接管预填”，提示条保留但不影响；
4. 创建成功后回到默认状态，避免下一次进入仍是旧代码。

回测预填：现有 `view.ts.backtest.codes` 是字符串，直接 `setBacktest({ codes: code })` 即可，无需改
Backtest 页逻辑；但要在 Backtest 页展示来源提示（P2：“代码来自自选 · 600519.SH”）。

### 4.5 备注内联编辑

交互规格：

| 事件 | 行为 |
|---|---|
| 进入编辑 | 点击备注单元格（或 `⋯` → 编辑备注）；单元格变为 `<input class="input w-full">`；焦点进入并全选；`aria-label="编辑 <code> 的备注"` |
| 输入限制 | `maxLength=256`（与 DB 列一致）；超过 200 字符显示计数 `214/256`；纯空格保存时 trim 成空串 |
| 保存 | `Enter` 或失焦且内容变化：`POST /watchlist`（`{code,list_name,note}`）；乐观更新当前列表；`input` 禁用 + 显示“保存中…” |
| 取消 | `Esc`：恢复原值、退出编辑、焦点回到触发元素；不发送请求 |
| 未变化 | 失焦不发送请求，直接退出编辑 |
| 失败 | 保持编辑态，单元格下方 `text-xs text-red-400` “保存失败：<原因> [重试]”；`409 quota_exceeded` 理论上不会发生（更新已有条目不计配额），若发生按网络失败处理 |
| 并发 | 无版本字段，后写覆盖先写（last-write-wins）；两人同时编辑同一行以最后保存为准，刷新后可见 |
| 空备注 | 允许保存空串（清空备注）；显示“添加备注”灰字 |

线框图：

```text
查看态：
│ 备注                                         │
│ 等待回调                         [✎]         │

编辑态：
│ 备注                                         │
│ [等待回调后加仓______________] 12/256        │
│ Enter 保存 · Esc 取消                        │
```

权限：viewer 下备注显示为纯文本，不进入编辑；`title="查看者无权修改备注"`。更新使用**现有** `POST`
端点，不新增 `PATCH`：幂等键 `(list_name, code)` 已由唯一约束保证。若未来引入“备注版本/审计”，再
评估 `PATCH /watchlist/{id}`（见 §10）。

### 4.6 批量选择与批量删除

- 表头复选框全选**当前筛选结果**（不含被搜索隐藏的行）；部分选中显示 indeterminate（`aria-checked="mixed"`）；选中键为条目 `id`，刷新后用 id 求交集，已消失行自动剔除。
- 切清单、改搜索词时清空选择（避免“删了看不见的行”）；仅排序时保留；选中后工具条：`已选 2 项（共 12） [批量删除] [取消选择]`（移动端固定在表格下方）。
- 危险确认（沿用总纲 §6.2.C）：

```text
┌ 移出 2 只自选？────────────────────┐
│ 600519.SH 贵州茅台                │
│ 000001.SZ 平安银行                │
│ 将从「核心组合」移除，备注一并删除。│
│ 行情、预警规则、回测历史不受影响。 │
│ [取消]                [移出 2 只] │
└───────────────────────────────────┘
```

- 提交 `POST /watchlist/batch-delete {ids:[...]}`【拟新增】；成功后：
  - 全成功：toast“已移出 2 只”；
  - 部分成功（并发下他人已删）：“已移出 2 只；1 只已不存在，已刷新列表”；
  - 失败：toast 错误 + 保留选择，可重试。
- 结果汇总必须用 `role="status" aria-live="polite"`，不能只靠颜色。
- 「清单内条目清空」与「删除清单」不同：批量删除后清单若变 0 条，该清单从列表消失（清单由行派生）。

### 4.7 CSV 导入 / 导出

#### 4.7.1 列定义与编码

| 列 | 表头 | 必填 | 说明 |
|---|---|---|---|
| 代码 | `code` | 是 | 接受 `600519` / `sh600519` / `600519.SH` 等；服务端 `normalize()` 规范化；非法代码逐行报告 |
| 名称 | `name` | 否 | 仅作人读；导入时忽略（名称以 `instruments` 为准，避免与主数据冲突） |
| 备注 | `note` | 否 | ≤256 字符；可含逗号（按 CSV 规则加引号） |

- 编码：UTF-8 with BOM（`\uFEFF`），Excel 打开不乱码；导入接受无 BOM；换行导出 `\r\n`、导入接受
  `\r\n`/`\n`；字段含 `,` `"` 或换行时双引号包裹，内部 `"` 写成 `""`。
- 公式注入防护（导出）：备注以 `= + - @` 开头时前缀 `'`，避免 Excel 当公式执行；导入时原样保留。
- 文件名：`watchlist-<清单名>-YYYY-MM-DD.csv`（非法文件名字符替换为 `_`）；单次导入 ≤2000 行、
  文件 ≤1 MB（前端先拦截，服务端同样 422）。
- 导出实现【增量】：前端用已加载清单生成 CSV + `Blob` 下载，不新增导出端点；只有未来出现分页/大
  清单（>2000 行）再考虑 `GET /watchlist/export`【拟新增，暂不做】。

导入弹层（桌面居中 modal / 移动全屏 sheet）：目标清单选择 + 文件选择 + `[x] 跳过已存在` +
`[ ] 覆盖备注` + 前 20 行预览表（行号/代码/名称/备注/状态：待新增、已存在→跳过、非法代码）+ 预计
计数 + 配额警示 + `[取消] [导入 N 行]`。

导入交互规格：

1. 选择文件 → 前端解析（`FileReader` + 轻量 CSV 解析，不新增依赖）→ 预览 + 行级校验；
2. 前端校验表头（`code,name,note` 或中文 `代码,名称,备注`）、文件内重复（仅首次生效）、空代码/
   超长备注；非法行在预览中标红但不阻止提交（交由服务端逐行报告）；
3. 提交 `POST /watchlist/import`【拟新增】（§5.2.5）；服务端返回的 `invalid[]` 下标换算成 CSV 行号
   （`index + 2`，含表头）；成功后弹层替换为结果“新增 3 · 更新 0 · 跳过 1 · 非法 1 / 第 4 行 abc
   （无法识别证券代码）”+ `[查看「核心组合」→]`；
4. 失败：`409 quota_exceeded` 显示“配额不足：8/10，本次需新增 3 只”并提示减少行数（**不写入任何行**）；
   `422` 展示 detail；网络失败保留预览可重试。

#### 4.7.3 去重与配额策略

| 情况 | 默认（`skip_existing=true`） | 覆盖模式（`skip_existing=false`） |
|---|---|---|
| 清单内已存在同 code | 计入 `skipped`，不改备注 | 计入 `updated`；CSV 有备注则覆盖，无备注列值则**保留原备注**（避免误清空） |
| 文件内重复 code | 首次生效，后续计入 `skipped`（reason=`duplicate_in_request`） | 同左（只保留首次出现的备注） |
| 非法代码 | 不写入，进入 `invalid[]` | 同左 |
| 配额 | 一次校验“本次真正新增”的条数；不足则整批 409，不部分写入 | 同左 |

### 4.8 选股结果 → 自选（分析页 / 策略页）

#### 4.8.1 页面改动

分析页（`Analyze.tsx:197-239`）与策略页运行结果（`StrategyConfigs.tsx:473-508`）做同样两处改动：

```text
┌ 多因子选股 ───────────────────────────────────────────┐
│ 最低综合评分 [1.0] [运行筛选]   已选 2 [全部加入自选] │
│ [ ] 代码    名称  评分 动量20 … 操作                  │
│ [ ] 600519 茅台  1.82 +4.1%     [+ 加入自选]          │
│ [ ] 000001 平安  1.55 +2.0%     [+ 加入自选]          │
└───────────────────────────────────────────────────────┘
```

策略页同理：结果表上方右侧 `[全部加入自选]`，表尾加“操作”列 `[+]`；两处共用同一个
`AddToWatchlistDialog` 组件。

交互：

| 入口 | 行为 |
|---|---|
| 行级 `[+]` | 打开弹层，默认勾选该行；确认后只加入 1 只 |
| 工具栏 `全部加入自选` | 弹层默认全选当前结果（`items` 全部，最多 500，现状 `limit` 上限） |
| 弹层“目标清单” | `(○) 已有清单 ▾`（P0：仅 default + 本地已知；P1：`GET /watchlist/lists`）；`(○) 新建清单 [输入]` |
| 已存在处理 | `[x] 已存在的跳过` 默认勾选；取消勾选 = 用选股结果覆盖备注为 `选股 <trade_date>`（可选） |
| 提交 | `POST /watchlist/import`【拟新增】；按钮显示“加入 N 只”，pending 时禁用 |
| 成功反馈 | 弹层原地替换为结果：`新增 3 · 更新 0 · 跳过 1 · 非法 0`；主按钮 `[查看清单 →]` 跳 `/watchlist` 并选中该清单；次按钮 `[继续挑选]` 关闭弹层 |
| 无结果 | 结果表本来就为空时不显示“加入自选”按钮 |
| viewer | 两处按钮禁用 + `title="查看者无法修改自选"`；页面顶部沿用现有只读提示（策略页已有 `StrategyConfigs.tsx:106-110`） |

弹层内容：勾选清单（前 3–5 只展示代码/名称）+ `(●) 已有清单 ▾ / (○) 新建 [___]` + `[x] 已存在的
股票跳过` + `[取消] [加入 N 只]`；提交后原地替换为结果与 `[查看清单 →]`。

P1 前降级（无 `import` 端点时）：前端按当前清单预取一次代码集合，逐条 `POST /watchlist`（并发 ≤ 4），
自行统计 added/skipped；超过 20 只时提示“批量接口上线前建议减少数量”。这是**过渡态**，必须标注
“P1 后移除”，不得把它当成正式批量能力（总纲 §0：前端假装批量不可接受）。

### 4.9 Dashboard 自选卡片升级

现状：只显示 default 前 8 只的代码/名称，链接错误（`Dashboard.tsx:99-122`）。目标【增量】：卡片标题
`自选股 · <当前清单>`，表格列 = 代码/名称/最新价/涨跌幅（P2；P1 前行情列 `--`），最多 8 行，底部
“共 N 只 · 数据 YYYY-MM-DD 收盘”；“管理/查看全部”→`/watchlist`；quotes 失败只显示 `--` 不报红；
切工作区由路由 key 自动重取。

### 4.10 只读（viewer）视角

- 顶部 amber 提示：“当前工作区为只读角色（viewer）：可查看清单、行情与备注，不能新增、编辑、导入或
  删除自选。”
- 写操作入口**保留可见但禁用**（`disabled` + `title`）：新增/导入/批量删除/备注编辑/加预警/去回测；
  复选框与全选禁用；允许切换清单、查看 K 线、导出 CSV、打开预警页预览（预览对 viewer 开放）。
- 服务端仍是最终裁决：绕过前端也会得到 `403` + `X-Auth-Error: read_only`（§6）。
- 匿名：门禁关闭时无 viewer 概念，`canWriteWorkspace(null) === true`（`workspace.ts:83-86`），单机可写。

## 5. 后端缺口与拟新增契约（关键）

### 5.1 缺口总表

| # | 能力 | 【现状】可复用 | 缺口/影响 | 【拟新增】或降级 | 阶段 |
|---|---|---|---|---|---|
| 1 | 枚举清单 + 计数 | `GET /watchlist?list_name=` 只能取一个清单 | 左侧清单栏无法实现；空清单与不存在不可区分 | `GET /watchlist/lists` | P1 |
| 2 | 清单重命名 | 无（只能逐条删再加） | 改名单 = 丢备注/丢顺序 | `PATCH /watchlist/lists/{list_name}` | P1 |
| 3 | 整单删除 | 无逐条删除以外的能力 | 大批量清理需 N 次请求 | `DELETE /watchlist/lists/{list_name}` | P1 |
| 4 | 批量删除 | `DELETE /watchlist/{id}` 一次一条 | N 次往返、部分失败不可汇总 | `POST /watchlist/batch-delete` | P1 |
| 5 | 批量新增/选股入自选 | `POST /watchlist` 单条、返回 201 | 50 只 = 50 次请求；配额分散检查；无逐行报告 | `POST /watchlist/import` | P1 |
| 6 | 批量行情 | `GET /stocks/{code}/bars`（P0 兜底）、`/snapshot`（单只） | 逐只快照会放大成 N 次全市场拉取 | `GET /market/quotes?codes=` | P2 |
| 7 | 批量评分 | `GET /analysis/factors/{code}` 单只 | N 次 HTTP；评分列无法低成本展示 | `GET /analysis/factors?codes=` 或隐藏评分列 | P2 |
| 8 | 备注更新 | **幂等 `POST /watchlist` 已足够**（同 list+code 更新 note） | 状态码恒 201，前后端靠预判区分 | 不新增端点；前端预判 + 说明 | P0 |
| 9 | 加预警 | `GET /alerts/types`、`POST /alerts/preview`、`POST /alerts/rules` | 无“自选预填”入口 | 前端预填（§4.4），**不新增端点** | P0 |
| 10 | 回测 | `POST /backtest/run`（`codes`/`screening_id`） | 无“从自选带入” | 前端预填 `codes`，**不新增端点** | P0 |
| 11 | 选股结果 | `POST /analysis/screen`、`POST /strategy/screenings/{id}/run` | 无“加入自选”动作 | 复用现有查询 + `import` | P1 |
| 12 | 数据日期/行情状态 | `GET /system/status`、`GET /market/overview` | 无个股 stale 标记 | P0 按 bars 日期推算；P2 quotes 提供 | P0/P2 |
| 13 | 配额展示 | `GET /workspaces/{id}/stats`（`usage/limits`） | 仅在隔离开启时可用 | 复用；隔离关闭不展示配额 | P1 |
| 14 | 清单引用检查 | `GET /alerts/rules`、`GET /strategy/samples` | 无服务端引用计数 | 前端读两类配置做提示（best effort） | P1 |

**明确不新增**：导出端点（前端生成 CSV）、备注 PATCH、清单计数单独端点、预警/回测的“自选专用”端点。
理由分别是：数据已在内存；幂等 POST 已覆盖语义；计数随 lists 返回；后两者只是预填，不改变写入契约。

### 5.2 拟新增端点契约

所有端点遵循现有风格：`FastAPI` + Pydantic `response_model`，错误体统一 `{"detail": "可读中文"}`，
写端点用 `require_editor`，读端点用 `current_workspace`，隔离过滤统一走 `db.scoped.in_workspace`。
新建/修改的 schema 放在 `src/stockmaster/schemas/__init__.py` 的 Watchlist 段落附近；路由可加在
`src/stockmaster/plugins/system/routers/system.py`（文件仅 290 行，暂不必拆 router）。

#### 5.2.1 `GET /watchlist/lists`

- 权限：`current_workspace`（viewer 可读）；隔离关闭时统计 0 号共享桶。
- 语义：按 `(workspace_id, list_name)` 分组，返回清单名、条目数、最近添加时间；**没有行的清单不存在**；
  当工作区无任何行时仍合成一条 `default`（`count=0`）供 UI 落地。
- 排序：`default` 固定第一，其余 `count DESC, list_name ASC`。

```json
[
  { "list_name": "default", "count": 8, "last_added_at": "2026-10-17T11:02:31" },
  { "list_name": "核心组合", "count": 12, "last_added_at": "2026-10-16T09:20:00" }
]
```

错误：`401`（门禁开启且未登录，中间件）、`400 bad_workspace`（`X-Workspace-Id` 非整数）、
非成员由 `workspace_ctx` 统一 `404`。无其他业务错误。

#### 5.2.2 `PATCH /watchlist/lists/{list_name}`

- 权限：`require_editor`；viewer → `403` + `X-Auth-Error: read_only`。
- 请求：`{"new_name": "核心持仓"}`；路径参数需 URL 编码（中文/空格）。
- 响应：`{"old_name": "核心组合", "new_name": "核心持仓", "updated": 12}`。
- 实施：先校验新名（trim 后 1–64 字符、无控制字符），再查目标名是否已存在（同工作区）→ 409；
  最后单条 `UPDATE watchlist_items SET list_name=:new WHERE workspace_id=:ws AND list_name=:old`。
  不改变配额（条目数不变）、不自动更新 `AlertRule.scope_value` / `SampleConfig.universe_value`。

| 状态 | 头/错误 | 触发 |
|---|---|---|
| 200 | — | 成功（`updated` 可为 0 之外的正常值） |
| 403 | `X-Auth-Error: read_only` | viewer |
| 404 | — | 旧清单不存在（含已被他人删空） |
| 409 | — | 目标清单名在同一工作区已存在 |
| 422 | — | 新名为空 / 超 64 字符 / 与原值相同（可选：同值直接 200） |
| 400 | `X-Auth-Error: bad_workspace` | `X-Workspace-Id` 非整数 |

#### 5.2.3 `DELETE /watchlist/lists/{list_name}`

- 权限：`require_editor`。
- 响应：`{"list_name": "观察池-1017", "deleted": 6}`；清单不存在 → `404`（不能靠 DELETE 空清单探测存在性）。
- 语义：删除该工作区该清单全部行，返回删除条数；释放配额；不级联删除预警/回测/样本（它们只保存名字）。
- 引用检查由前端完成并提示（§4.2）；服务端**不**因被引用而拒绝（否则前端无法解释“谁引用了”）。
  若未来需要强一致，可加 `?dry_run=true` 返回引用数【P2 可选】。

#### 5.2.4 `POST /watchlist/batch-delete`

- 权限：`require_editor`。
- 请求：`{"ids": [101, 102, 103]}`（1–500 个，去重后处理）。
- 响应：`{"deleted": 2, "missing": [103]}`；`missing` 包含不存在或属于其他工作区的 id（**不区分**，
  与现有 404 语义一致，避免枚举）。
- 语义：单事务删除当前工作区命中的所有 id；不做逐条错误。`422`：ids 为空、超过 500、非整数。
- 成败语义是“部分成功”：HTTP 200 即代表请求有效，调用方用 `deleted/missing` 判断。

#### 5.2.5 `POST /watchlist/import`（批量新增/upsert）

- 权限：`require_editor`；viewer 403 read_only。
- 用途：选股结果入自选、CSV 导入共用。**替代 N 次 `POST /watchlist`**。
- 请求体：

```json
{
  "list_name": "观察池-1017",
  "codes": ["600519.SH", "abc", "000001", "300750.SZ"],
  "notes": { "600519.SH": "2026-10-17 选股：评分 1.82" },
  "create_list": true,
  "skip_existing": true
}
```

- 响应 200（含部分成功）：

```json
{
  "list_name": "观察池-1017",
  "added": 2,
  "updated": 0,
  "skipped": 1,
  "invalid": [{ "index": 1, "code": "abc", "reason": "invalid_code", "message": "无法识别证券代码" }],
  "list_created": true,
  "total_items": 42,
  "quota": { "resource": "watchlist_items", "limit": 100, "used": 42, "remaining": 58 }
}
```

- 字段语义：

| 字段 | 规则 |
|---|---|
| `codes` | 1–2000 个；逐个 `normalize()`；空串/无法识别进 `invalid[]`（`index` 为原数组下标，前端换算 CSV 行号）；不中断整批 |
| `notes` | 可选，key 为**规范代码**（`600519.SH`）；值 ≤256；同一个 code 只取第一条；未知 key 忽略 |
| `list_name` / `create_list` | 清单名同 5.2.2 校验（1–64 字符）；`create_list` 默认 `true`，清单由首行隐式创建；`false` 且清单不存在 → **404 `list_not_found`** |
| `skip_existing` | 默认 `true`：已存在计入 `skipped`，不改备注；`false`：已存在计入 `updated`，有 `notes[code]` 时覆盖，否则保留原备注（避免误清空）；文件内重复只首次生效 |
| 配额与事务 | **只对真正新增的条数做一次** `quota.enforce(..., amount=new_count)`，不足 → 整批 409 且不写任何行；解析 → 查重 → 配额 → 插入/更新 → 一次 commit，失败回滚 |

- 实现提示：先 `SELECT code, note FROM watchlist_items WHERE workspace_id=:ws AND list_name=:name AND code IN (...)`；
  `new_count` = 去重后不在集合中的数量；**先配额后写**（与 `backtest/run` 的“先配额再计算”一致）。
- 错误：`403` + `X-Auth-Error: read_only`（viewer）；`404`（`create_list=false` 且清单不存在）；
  `409` + `X-Auth-Error: quota_exceeded`（`detail` 同 `quota.enforce`，含上限）；`422`（codes 为空/超
  2000、清单名非法、notes 超长）；`400` + `X-Auth-Error: bad_workspace`。成功/部分成功均为 200。

#### 5.2.6 `GET /market/quotes?codes=...`

- 权限：与 `/market/*` 其他端点一致，**不依赖工作区**（行情是共享参考数据，不按工作区隔离）；
  门禁开启时仍受中间件保护。`codes` 必填，逗号分隔，1–200 个，`normalize()` 去重；非法整体 `422`。
- 一次请求内批量取价，**禁止**前端逐只调用 `/stocks/{code}/snapshot`，原因见下。

```json
{
  "as_of": "2026-10-17",
  "source": "snapshot",
  "items": [
    {
      "code": "600519.SH", "name": "贵州茅台", "price": 1680.0, "preclose": 1660.0,
      "pct_chg": 1.2, "amount": 1230000000.0, "turnover": 0.35, "trade_date": null,
      "quote_time": "2026-10-17T14:55:00", "source": "snapshot", "stale": false
    }
  ],
  "missing": ["000001.SZ"]
}
```

- 数据来源与兜底：
  1. 优先 `registry.fetch_snapshot(codes)`：provider 内部先拉全市场再过滤（`sina_provider.py:243-303`），
     **一次请求只应触发一次市场级拉取**；返回 `price/pct_chg/amount/turnover/quote_time/source`；
  2. 快照不可用（`ProviderUnavailable`/503）时，回退到已存日线：`raw` 优先、无则 `qfq`，取最新一根
     `close/preclose/pct_chg/amount/turn`，`trade_date=<bar.trade_date>`、`source="daily"`，
     响应头 `X-StockMaster-Reason: snapshot-unavailable`【拟新增】；
  3. 个股既无快照也无日线 → 放入 `missing`；全部都没有 → `503`（`detail` 说明数据未同步/供应商不可用）。
- `stale` 判定：`source=daily` 且 `trade_date <` 全市场最新 qfq 日线日期（用 `MAX(daily_bars.trade_date)`，
  与 `/market/overview` 同口径）→ `true`；快照 `quote_time` 为空也置 `true`。UI 只提示“数据截至”，
  不把日线数据包装成“实时”。
- 性能保护：单次外部市场快照在服务端做 3–5 秒进程内缓存（同一时刻合并并发请求），避免自选页刷新
  连续打数据源；`codes` 过滤在缓存结果上做，不按 codes 分别缓存。

**为什么逐只 `/stocks/{code}/snapshot` 不可行**（实现说明必须写进代码注释）：

1. 每次调用都会触发一次**全市场**快照下载（`sina_provider.py:243-303` 的 `stock_zh_a_spot()`），
   20 只 = 20 次全市场请求；异常还会 `record_failure()`（`providers/registry.py:159-163`），连续
   失败可能熔断供应商，影响全站同步与预警。
2. 单只响应不含换手率/`trade_date`/`stale`（`market.py:268-280`），无法支撑行情列；前端并发
   `Promise.all` 也容易触发限流，且部分失败只能靠 `allSettled` 拼装，状态码语义丢失。

#### 5.2.7 `GET /analysis/factors?codes=...`（批量评分，P2）

- 权限：共享参考数据，不依赖工作区；`codes` 1–200。
- 响应：`{"trade_date": "2026-10-17", "items": [{ "code": "600519.SH", "score": 1.82,
  "trade_date": "2026-10-17" }], "missing": []}`；无因子数据时 `trade_date=null, items=[]`（与
  `/analysis/screen` 的空结果语义一致），不 404。
- 评分列在 P2 前可隐藏；P1 的临时方案是逐只调用现有 `GET /analysis/factors/{code}`（本地表查询，
  无外部请求，但仍 N 次往返，且 404 代表“未跑分析任务”）。

### 5.3 兼容性、迁移与限流

- 全部为**新增路由**，不改 `GET/POST/DELETE /watchlist` 的请求/响应，不新增表、不新增迁移；
  P0/P1 均可安全灰度（旧前端不知道新端点）。
- 清单仍是 `list_name` 派生物：不引入 `watchlist_lists` 表；需要持久化空清单/排序时再评估（§10）。
- 批量上限：import 2000、batch-delete 500、quotes/factors 200——用 422 明确拒绝而不是截断。
- 写操作保持“先校验/配额、再写库、一次 commit”的顺序；配额检查与写入之间无锁，并发可能短暂超出
  1–2 条（`core/quota.py:14-16` 已声明可接受），批量导入必须复用该语义而不是逐条检查。

---

## 6. 权限、隔离与配额

### 6.1 角色矩阵（隔离开启时）

| 操作 | owner | editor | viewer | 匿名（门禁关） | 匿名（门禁开） |
|---|---|---|---|---|---|
| 查看清单/行情/备注/导出/预览 | ✅ | ✅ | ✅ | ✅（0 号桶） | ❌ 401 |
| 新增（含导入）/备注编辑/删除/批量删除/重命名/删除清单 | ✅ | ✅ | ❌ 403 | ✅（0 号桶） | ❌ 401 |
| 加预警（创建规则） | ✅ | ✅ | ❌ 403 | ✅ | ❌ 401 |
| 运行回测 | ✅ | ✅ | ❌ 403 | ✅ | ❌ 401 |
| 运行选股 | ✅ | ✅ | ✅ | ✅ | ❌ 401 |

依据【现状】：`require_editor` 拒绝 viewer（`workspace_deps.py:139-147`）；`GET /watchlist` 用
`current_workspace`；`POST /alerts/preview`、`POST /strategy/screenings/{id}/run` 刻意保持只读；
`POST /backtest/run`、`POST /alerts/rules` 为写操作。前端 `canWriteWorkspace()` 只做显隐/禁用，
服务端仍必须返回 403。

### 6.2 隔离与归属

| 场景 | 后端行为 | UI 表现 |
|---|---|---|
| 隔离开启，请求头 `X-Workspace-Id: N` 且是成员 | 按 N 过滤；角色进入 `WorkspaceCtx` | 工作区条显示角色；自选页数据随切换重取 |
| 隔离开启，非成员/不存在的工作区 | 统一 `404`（`workspace_not_found`），不泄露存在性 | “工作区不存在或你无权访问”，回个人工作区 |
| 隔离开启，头非法（非整数） | `400` + `X-Auth-Error: bad_workspace` | 提示切换工作区；不静默落到个人工作区 |
| 隔离开启，跨工作区 item id | `DELETE` 404；批量删除计入 `missing` | 视为“已被他人删除”，刷新列表 |
| 隔离开闭 | 所有数据落 `workspace_id=0`；`in_workspace` 恒真；`workspace_ctx.id=0` | 不显示工作区切换器；无配额限制；所有人共享一份 |
| 隔离关闭 + 门禁关 | 匿名 = editor（`workspace_deps.py:91-98`） | 单机可写；无只读提示 |

### 6.3 错误头与前端处理

| HTTP | `X-Auth-Error` / 标识 | 来源 | 前端处理 |
|---|---|---|---|
| 401 | 门禁中间件 body `code`（`unauthorized` 等） | `auth/middleware.py:76-82` | `api.ts` 清 token 并广播 `unauthorized`；`SessionGate` 跳登录 |
| 403 | `read_only` | `require_editor` | 禁用写按钮 + amber 提示“查看者无法修改”；toast 展示 detail |
| 409 | `quota_exceeded` | `quota.enforce` | 显示“已达上限（N 条）”；导入改为减少行数；提示联系 owner |
| 400 | `bad_workspace` | `workspace_ctx` | 提示切换工作区（通常前端状态已失效） |
| 404 | `workspace_not_found` 等 | 工作区解析/资源不存在 | 非成员提示；单条自选删除按“已不存在”处理 |
| 503 | — + `X-StockMaster-Reason: snapshot-unavailable` | quotes 降级失败 | 顶部行情降级横幅；清单操作继续可用 |
| 404 | `X-StockMaster-Reason: data-not-synced` | bars 未同步 | 行情列显示 `--` + “未同步”；引导设置页同步 |

**代码缺口**：`frontend/src/api.ts` 的 `ApiError` 只有 `status/detail`，没有暴露响应头；
在补齐 `code` 字段前，前端只能按 status + detail 文案判断，无法稳定区分 `read_only` 与普通 403。

### 6.4 配额规则

- 资源键 `watchlist_items`；`SM_WORKSPACE_MAX_WATCHLIST_ITEMS=0`（默认）表示不限；仅隔离开启且
  `workspace_id>0` 生效。
- 只有**新增行**消耗配额；幂等 `POST` 更新、`import` 的 `updated`、批量删除均不消耗；删除释放配额。
- 错误：`409` + `X-Auth-Error: quota_exceeded`，`detail` 已含资源名与上限（如“工作区自选股已达上限
  （10 条）…”），前端直接展示；批量导入若返回该错误，**不写入任何行**。
- 配额展示：工作区启用时用 `GET /workspaces/{id}/stats` 的 `usage.watchlist_items` 与
  `limits.watchlist_items`（`0` 显示“不限”）；隔离关闭时无此端点语义，不展示配额卡片。

---

## 7. 状态矩阵

| 状态 | 触发 | 自选页表现 | 文案/恢复 |
|---|---|---|---|
| 首次加载 | 查询未返回 | 清单栏 3 行骨架、表格 6 行骨架；工具栏输入禁用 | 加载中… |
| 空清单/无任何清单 | `items=[]` 或 `lists` 仅 default(count=0) | 表体空态 + 左栏 default/草稿 | “「核心组合」还没有股票。[去行情页添加] [导入 CSV]” / “还没有自选。[去行情页挑几只 →]” |
| 清单不存在 | 切换/重命名后 404 | 刷新 lists 并回退 default | “清单已不存在，已切换到 default” |
| 清单加载失败 | `GET /watchlist/lists` 网络/5xx | 左栏错误态 + 重试；右表仍可用 | “清单加载失败：<原因> [重试]” |
| 条目加载失败 | `GET /watchlist` 失败 | 表体错误态；不自动清空已显示数据（有缓存时） | “自选加载失败：<原因> [重试]” |
| 数据未同步 | bars 404 `data-not-synced` | 行情列 `--`；行内“未同步” | “该股日线尚未同步，去设置页同步” |
| stale | 个股日期落后全市场 | 价格旁 amber “旧数据”badge | tooltip “最后日期 2026-10-15”；同步入口 |
| 行情降级 | quotes 503 / 回退日线 | 顶部 amber 横幅；价格列仍显示日线值 | “行情暂不可用，已显示 2026-10-17 收盘数据 [重试]” |
| 配额超限 | 新增/导入 409 | 表单/弹层内错误；不出现“加了一半” | “工作区自选已达上限（N 条），请先清理或联系管理员” |
| 只读 viewer | 角色 viewer | 顶部 amber 只读条；写按钮禁用 | “可查看清单、行情与备注，不能新增、编辑、导入或删除” |
| 批量部分成功 | batch-delete 返回 missing | toast + 刷新 | “已移出 2 只；1 只已不存在，已刷新列表” |
| 导入部分成功 | invalid[] 非空 | 结果弹层列出非法行 | “新增 3 · 跳过 1 · 非法 1；第 4 行 abc 无法识别” |
| 会话过期 | 401 | 跳登录并带 `next=/watchlist` | “登录已过期，请重新登录” |
| 工作区头失效 | 400 bad_workspace | 提示并刷新工作区列表 | “工作区状态已变化，请重新选择” |

---

## 8. 响应式与可达性

### 8.1 响应式

| 断点 | 布局 |
|---|---|
| `<640px` | 清单栏折叠为 `[当前清单 ▾]`；表格 `.table-wrap` 横向滚动；隐藏“名称/备注”列（备注进 `⋯`）；批量条固定在表格下方；操作全部进 `⋯` 菜单 |
| `640–1279px` | 单列；清单栏改为表格上方的横向 chips（`flex overflow-x-auto`），或保留下拉；表格完整列但横向滚动 |
| `≥1280px` | `grid grid-cols-[260px_1fr]`；清单栏内部滚动；表格完整列，操作按钮常显 |
| 粘性 | 表头 `sticky top-0`（在 `.table-wrap` 内）；窄屏代码列 `sticky left-0 bg-slate-900`，保证横向滚动时仍能识别行 |

### 8.2 可达性

| 主题 | 要求 |
|---|---|
| 键盘 | Tab 顺序：清单栏 → 工具栏 → 表头复选 → 行复选 → 行操作；`Space` 切换复选；`Enter` 打开行内编辑/弹层；`Esc` 取消编辑/关闭弹层；弹层打开时焦点进入首个控件，关闭回到触发元素 |
| 表格语义 | `<table><caption class="sr-only">核心组合自选行情</caption>`；`th scope="col"`；可排序表头 `aria-sort`；数字列 `text-right` 但读屏仍按文本 |
| 复选 | 每行 `aria-label="选择 贵州茅台 600519.SH"`；全选 `aria-checked="mixed"`；禁用时 `aria-disabled` |
| 弹层 | `role="dialog" aria-modal="true" aria-labelledby`；焦点陷阱；确认按钮不是默认焦点（防误删） |
| 反馈 | toast/结果汇总用 `role="status" aria-live="polite"`；错误用 `role="alert"`；全选计数变化播报 |
| 颜色 | 涨跌 = 颜色 + `+/-` 符号 + `aria-label="涨 1.20%"`；只读/stale 用文字 badge 而非仅颜色 |
| 触控/点击 | 行内按钮 ≥32px；移动端菜单项 ≥44px；复选框点击区域覆盖整格；**行本身不响应点击**（不会既选中又跳转），只有复选框改变选中、代码单元格是按钮，行内操作 `stopPropagation`；新增骨架/展开尊重 `prefers-reduced-motion` |

---

## 9. 实施拆分与验收

### 9.1 阶段拆分

| 阶段 | 交付 | 前端文件 | 后端文件 | 验收（可观察行为） |
|---|---|---|---|---|
| **P0** | `/watchlist` 最小可用：单清单（default + 本地已知名）、行情列（日线兜底）、备注内联编辑、单条移出、K线/预警/回测跳转、Dashboard 链接修正 | `pages/Watchlist.tsx`（新）、`App.tsx`、`api.ts`、`types.ts`、`view.ts`、`Dashboard.tsx` | 无 | `pnpm build` 通过；加入→刷新仍在；备注保存后重进一致；viewer 按钮禁用；`/market` 加入自选仍可用 |
| **P1** | 多清单管理（枚举/计数/新建/重命名/删除/引用提示）、批量选择+删除、选股结果入自选（分析页+策略页） | `Watchlist.tsx`、`components/AddToWatchlistDialog.tsx`（新）、`Analyze.tsx`、`StrategyConfigs.tsx`、`Market.tsx`、`Settings.tsx` | `plugins/system/routers/system.py`、`schemas/__init__.py` | lists 返回计数；改名后条目全在新名；删除清单后 lists 消失且配额释放；选股 50 只一次加入，反馈 added/skipped；viewer 403 不出现半写 |
| **P2** | 批量行情与评分列、CSV 导入/导出、Dashboard 行情升级、回测整个清单 | `Watchlist.tsx`、`Dashboard.tsx`、`csv.ts`（新）、`types.ts` | `plugins/market/routers/market.py`、`plugins/analysis/routers/analysis.py`、`schemas/__init__.py` | 20 只行情 1 次请求；供应商不可用时回退日线并标 stale；评分列与因子日一致；导出再导入 round-trip 无重复、非法行逐行报告 |

> 与总纲 §12 的关系：总纲按本文结论统一为「P1 只做清单/批量写端点，P2 再做批量行情与导入导出」，
> 不改变依赖关系——P2 依赖 P1 的 import 端点和 lists 端点。

### 9.2 测试矩阵（建议）

| 测试文件 | 用例（名称可直接使用） |
|---|---|
| `tests/test_watchlist_lists.py` | `test_lists_counts_and_default_first`、`test_lists_isolated_per_workspace`、`test_lists_empty_synthesizes_default`、`test_lists_non_member_404`、`test_lists_viewer_can_read` |
| `tests/test_watchlist_rename_delete.py` | `test_rename_updates_all_rows`、`test_rename_404_and_409`、`test_rename_viewer_403_read_only`、`test_delete_list_returns_count_and_frees_quota`、`test_delete_list_404`、`test_rename_does_not_touch_alert_scope` |
| `tests/test_watchlist_batch_delete.py` | `test_batch_delete_partial_missing`、`test_batch_delete_foreign_ids_are_missing`、`test_batch_delete_validation_422`、`test_batch_delete_viewer_403` |
| `tests/test_watchlist_import.py` | `test_import_adds_skips_and_reports_invalid`、`test_import_upsert_updates_note_only_when_provided`、`test_import_single_quota_check_atomic`、`test_import_duplicate_in_request`、`test_import_create_list_false_404`、`test_import_isolation_off_shared_bucket`、`test_import_max_codes_422` |
| `tests/test_market_quotes.py` | `test_quotes_snapshot_batch`、`test_quotes_daily_fallback_sets_stale`、`test_quotes_missing_and_503`、`test_quotes_validation_422`、`test_quotes_no_workspace_scope` |
| `tests/test_analysis_factors_batch.py` | `test_factors_batch_latest_score`、`test_factors_batch_missing_and_empty`；前端无测试框架（不新增依赖），`pnpm build` + §9.3 |

关键断言：`403` 必须同时断言 `X-Auth-Error == "read_only"`；配额用例断言 409 header 与“未写入任何行”；
隔离用例覆盖 `workspace_id=0` 与两个真实工作区；批量端点断言单次请求的请求计数（可用 provider 替身）。

### 9.3 手工验收清单（P0/P1）

1. 新建清单 → 加入 1 只 → 刷新仍在；删除最后一只 → 清单从栏中消失；备注 Enter 保存、Esc 取消。
2. viewer：只读条出现，写入口禁用，K 线与导出可用；工作区切换后清单/行情/评分无跨区残留。
3. 配额：`SM_WORKSPACE_MAX_WATCHLIST_ITEMS=2` 下加第 3 只 409，批量导入整批失败，删 1 只后可再加。
4. 选股→自选：两页各测行级/全部加入与重复加入 skipped；CSV 导出含 BOM、非法行报告行号。

---

## 10. 未决问题

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | 是否引入 `sort_order` 持久化手工排序？ | 需要新列 + 迁移 + 回填；P0 无拖拽 | 先不做；要做则加 `WatchlistItem.sort_order` 并只提供“上移/下移” |
| 2 | 是否引入 `watchlist_lists` 表持久化空清单/顺序？ | “空清单不保存”略反直觉 | 暂不引入，用“加入第一只才生效”提示；产品坚持再迁移 |
| 3 | 实时快照开关？ | 数据源熔断/限流风险 | 默认关闭；P2 提供开关并显示 `quote_time` |
| 4 | 清单数量上限？ | 清单由行派生，无独立上限 | 跟随 `watchlist_items` 配额；必要时加 `SM_WORKSPACE_MAX_WATCHLIST_LISTS` |
| 5 | 重命名/删除是否级联更新预警/样本引用？ | 不更新会让规则指向旧名/空清单（空清单退化为全市场） | 本期提示 + 阻止删除被引用清单；长期由服务端级联或禁止 |
| 6 | 批量导入配额不足时是否允许“先导入装得下的 N 条”？ | 原子失败不够友好 | 默认原子；前端提示“还可新增 M 只”，用户删行重试 |
| 7 | `score` 口径（因子综合评分 vs 选股默认 60 分门槛）？ | 两处阈值易混 | 列头写“因子评分”，tooltip 说明非百分制；P2 评估百分位 |
| 8 | CSV 是否支持 XLSX / 单机是否显示配额入口？ | 用户主要用 Excel；单机语义简单 | 本期只 CSV；单机不显示工作区/配额提示，清单功能照常 |
