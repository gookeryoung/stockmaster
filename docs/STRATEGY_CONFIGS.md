# 工作区级策略配置（选股 / 样本 / 算法）

> 状态：**已实施**（`0004_strategy_configs` 迁移 + `screening_configs` / `sample_configs` /
> `algorithm_configs` 三张工作区私有表 + `/api/v1/strategy/*` 路由 + 前端「策略」页）。
> 开关：`SM_DATA_ISOLATION`，默认 `false`（= 全部落 `workspace_id = 0`，无工作区语义，可随时回退）。
> 前置：工作区与角色体系（`docs/WORKSPACE.md`，`0003_workspaces`）；指标/因子数据（`analytics`）；
> 训练链路（`src/stockmaster/plugins/predict/services/train.py`）。
> 本文沿用 `docs/WORKSPACE.md` 的归属与角色规则，只描述新增的三类「策略配置」资源。

---

## 1. 为什么要把一次性表单变成配置

在此之前，选股、训练、回测各自在页面上填一次参数、提交一次请求：

- 选股在「分析」页填 `min_score / max_vol / 板块 / 条数`，结果随页面丢弃；
- 训练在「预测」页选算法与周期，超参用代码内置默认值；
- 回测在请求里手填 `codes`，与刚才选出来的股票池没有可引用的关系。

这带来三个具体问题：

1. **不可复用**：同一套「高分、低波动、排除 ST」的条件每天重填，选股、回测、训练各填一遍，
   三处表述很容易出现细微差异，却没人能指出差在哪；
2. **不可协作**：工作区共享数据之后，成员只能看到「回测跑了什么」，看不到「我们约定用什么
   选股口径」。参数靠口头或截图同步，换一个成员就失真；
3. **不可追溯**：回测历史里的 `params_json` 是一次请求的快照，不是可命名、可再次引用的定义。

因此把三类方法论落成**工作区内命名的配置**，与「一次执行」分离：

- **选股配置（screening）**：筛选条件 + 排序 + 条数上限；可一键运行，也可被样本/回测引用；
- **样本配置（sample）**：标的池来源 + 日期窗口 + 预测周期 + 最少历史 bar 数，回答「用什么数据训练」；
- **算法配置（algorithm）**：算法 + 超参数 + 绑定的样本，回答「用什么算法训练」。

命名配置的归属单位沿用 cndb 式 `Workspace` + `WorkspaceMember(owner/editor/viewer)`：
配置属于**工作区**而不是某个账号，谁能看、谁能改由角色决定（见 `docs/WORKSPACE.md` §2）。

两个设计取舍：

- **不建一张「万能策略表」再塞 JSON**。三类配置的生命周期与校验规则不同（样本要解析标的池、
  算法要校验算法名与超参），拆成三张职责单一的表，字段能被约束、能被列表查询、能各自演进而
  不互相牵连。
- **条件集合放 JSON 列**（`params_json`），不拆成子表。筛选字段会随因子能力演进增删，且这些
  条件**从不参与 SQL 过滤** —— 求值在应用层对全站共享的 `factors` 表完成，SQL 只需要按
  `workspace_id` 取配置。真正需要数据库兜底的只有两件事：归属与工作区内不重名。

## 2. 数据模型

### 2.1 三张表

三张表共用同一组「私有表」列语义（与 `watchlist_items` / `alert_rules` / `backtest_runs` 一致）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | 自增主键 |
| `workspace_id` | Integer NOT NULL，默认 `0` | 归属工作区；`0` = 未归属桶（隔离关闭时的全部数据、隔离前的历史数据），不指向任何工作区行，因此**不加外键** |
| `name` | String(64) NOT NULL | 用户识别配置的入口；**工作区内唯一** |
| `description` | String(256)，默认空串 | 用途备注 |
| `created_by` | Integer NULL | 创建者账号 id（审计用，不加外键：账号不硬删除，且鉴权关闭时数据要能独立存在） |
| `created_at` / `updated_at` | DateTime | 创建/更新时间，`updated_at` 自动 onupdate |

**`screening_configs`** 额外列：

| 列 | 类型 | 说明 |
|---|---|---|
| `params_json` | Text，默认 `{}` | 选股参数，JSON 结构见 §2.2 |

**`sample_configs`** 额外列：

| 列 | 类型 | 说明 |
|---|---|---|
| `universe_type` | String(16)，默认 `default` | 标的池来源：`watchlist` / `screening` / `codes` / `default` |
| `universe_value` | String(512) | 按 `universe_type` 解释：清单名 / 选股配置 id / 逗号分隔代码 / 空 |
| `start_date` / `end_date` | Date NULL | 显式日期窗口 |
| `lookback_days` | Integer NULL | 相对最新数据回看的自然日数（与显式日期互斥，见 §2.2） |
| `horizon` | Integer，默认 `5` | 预测/标签周期（多少根日线后的收益为标签） |
| `min_bars` | Integer，默认 `60` | 每只标的最少历史 bar 数；解析样本时只返回候选池，训练时按此门槛剔除历史不足的标的 |

**`algorithm_configs`** 额外列：

| 列 | 类型 | 说明 |
|---|---|---|
| `algo` | String(32)，默认 `hist_gbdt` | `hist_gbdt` / `random_forest` / `logistic`（与 `AVAILABLE_ALGOS` 一致） |
| `params_json` | Text，默认 `{}` | 超参数，JSON 结构见 §2.2 |
| `sample_id` | Integer NULL，FK `sample_configs.id` `ON DELETE SET NULL` | 绑定的样本配置；可空 = 默认宇宙 + 全量历史 + `min_bars=60` |
| `horizon` | Integer，默认 `5` | 训练标签周期 |
| `test_fraction` | Float NULL | 测试集占比；空 = 用全局配置 `SM_TEST_FRACTION` |

`sample_id` 的删除语义是**置空而不是级联删除**：样本被移除后，算法定义（算法名 + 超参）本身
仍然有效，只是暂时没有绑定样本；把算法配置一起删掉会让用户意外丢失命名。

### 2.2 JSON 约定

#### 选股参数 `screening_configs.params_json`

由 Pydantic 模型 `ScreeningParams` 校验、序列化，缺省字段回填默认值（写回时是完整对象，
不是稀疏 diff）。PATCH 传 `params` 时**整体替换**，不做深合并 —— 条件列表的「合并」没有
唯一合理的语义，整体替换更可预测。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `min_score` | float \| null | `null` | 综合因子分下限（`factors.score >= min_score`） |
| `max_vol` | float \| null（≥ 0） | `null` | 20 日波动率上限（`factors.vol_20 <= max_vol`） |
| `min_amount_log` | float \| null | `null` | 20 日成交额对数下限（流动性门槛） |
| `exclude_st` | bool | `true` | 排除 ST（按 `instruments.is_st` / 行情快照） |
| `boards` | list[str] | `[]` | 板块白名单，取值与 `instruments.board` 一致：`main`(主板) / `star`(科创板) / `chinext`(创业板) / `bse`(北交所) / `index`(指数)；空 = 不限 |
| `conditions` | list[object] | `[]` | 逐因子条件，见下 |
| `order_by` | str | `"score"` | 结果排序字段（因子列名） |
| `direction` | `"asc"` \| `"desc"` | `"desc"` | 排序方向 |
| `limit` | int，1–500 | `50` | 结果条数上限 |

`conditions` 中每项的结构：

```json
{"field": "mom_20", "op": ">=", "value": 0.5}
```

- `field`：因子列名，取值限定在 `screening.py` 的 `CONDITION_FIELDS` 白名单内：`score`、`mom_5/20/60/120`、
  `dist_high_252`、`vol_20/60`、`atr_pct`、`turnover_20`、`amount_log_20`、`amihud_20`、`ma_bull`、
  `breakout_20`、`rsi_14`、`z_mom_20/60`、`z_vol_20`、`z_turnover_20`、`z_rsi_14`、`pct_mom_20/60`；
  白名单而不是 `getattr` 任意属性，未知字段写入时即 422；
- `op`：`>=` / `<=` / `>` / `<` / `==` 五选一（后端 `ScreeningOp` 白名单）；
- `value`：有限数值（`NaN`/`inf` 直接 422）。

`order_by` 使用与 `conditions[].field` **相同的白名单**，非法排序字段写入时 422。

求值顺序固定为：**硬过滤**（板块 / ST / 三个门槛）→ 逐条 `conditions` → 按 `order_by` 排序
→ 截断 `limit`。这保证「条件相同、排序不同」不会得到不同候选集。

空值语义（与 `POST /analysis/screen` 保持一致）：

- 门槛类（`min_score` / `max_vol` / `min_amount_log`）遇到因子缺失时**不参与判定**，不等于误杀；
  例外是 `min_score`：分数都没有的标的不能声称「分数达标」，直接排除；
- 逐条 `conditions` 遇到缺失值视为**不满足**（要求 `mom_20 >= 0.5` 时，没有动量数据的标的不算通过）；
- 排序时 `None` 永远排在最后，`direction` 只影响非空值；
- 数据库里还没有因子快照时返回空结果（`trade_date = null`、`count = 0`），由前端提示先跑 `analytics`。

#### 算法超参 `algorithm_configs.params_json`

超参是扁平字典，键直接透传给 `stockmaster/model/models.py` 的 `build_estimator()`。**只接受数值**
（整数/浮点，布尔与字符串都会 422）；每个算法的可用键与取值范围由 `strategy/algorithm.py`
的 `PARAM_SPECS` 白名单校验，未知键、越界、类型错误一律在写入时 422，而不是等 sklearn
抛异常。不传的键用代码内置默认值，`random_state` 由服务端统一注入以保证可复现
（不允许在配置里覆盖随机种子）。

> 预测页的「页面内即时自定义」复用**同一份** `PARAM_SPECS` 白名单：`POST /predict/train`
> 接受可选的 `algo_params` / `features` / `test_fraction`，在训练前完成同样的校验后透传，
> 但不写入 `algorithm_configs`（一次性试算，不落库）。不传这三项时行为与旧版一致。

| 算法 | 可用超参（类型，范围） | 内置默认（未配置时） |
|---|---|---|
| `hist_gbdt` | `max_iter`(int, 10–5000)、`learning_rate`(float, 0.0001–1.0)、`max_depth`(int, 1–32)、`min_samples_leaf`(int, 1–10000)、`l2_regularization`(float, 0–100) | `300 / 0.05 / 4 / 50 / 1.0` |
| `random_forest` | `n_estimators`(int, 10–2000)、`max_depth`(int, 1–64)、`min_samples_leaf`(int, 1–10000) | `300 / 8 / 50`（`max_features=sqrt`、`class_weight=balanced_subsample` 固定为内置值，不开放配置） |
| `logistic` | `C`(float, 1e-6–1000)、`max_iter`(int, 100–20000) | `0.5 / 2000`（`class_weight=balanced` 固定为内置值） |

未知算法名在写入时即 422（`AlgorithmName` 枚举），不会等到训练才失败。PATCH 传 `params`
同样是整体替换；`sample_id: null` 表示显式解绑样本（不是「不修改」）。

#### 样本参数

`universe_type` 与 `universe_value` 的搭配在写入时校验并**规范化后回写**：

- `watchlist`：空值补成 `default`（清单名）；
- `screening`：必须是整数形式的选股配置 id，且该配置存在于同一工作区（否则 422
  `screening_not_found`）；删除被引用的选股配置后再解析同样 422；
- `codes`：按 `,`、`;`、中文逗号/顿号、空白拆分，逐只 `normalize()`，非法代码 422（不静默
  丢弃），去重后以半角逗号重新拼接存储；
- `default`：不使用 `universe_value`，落库为空串。

日期窗口有两条互斥路径：

- 显式 `start_date` / `end_date`；或
- `lookback_days`：从数据最新交易日向前回看 N 个**自然日**。

两者**同时设置直接 422**（`lookback_days 不能与 start_date/end_date 同时设置`）—— 让「以哪个
为准」变成隐式约定，两边会算出不同样本，是后续最难看懂的那类偏差。两者都不给 = 使用全量历史。
`end_date < start_date` 同样 422。

### 2.3 唯一约束与索引

| 表 | 唯一约束 | 索引 |
|---|---|---|
| `screening_configs` | `uq_screening_config_name (workspace_id, name)` | `ix_screening_configs_workspace (workspace_id)` |
| `sample_configs` | `uq_sample_config_name (workspace_id, name)` | `ix_sample_configs_workspace (workspace_id)` |
| `algorithm_configs` | `uq_algorithm_config_name (workspace_id, name)` | `ix_algorithm_configs_workspace (workspace_id)` |

- 工作区是唯一键的一部分，而不是「查询时再过滤」：否则两个工作区各有一个「默认选股」会互相覆盖；
- 重名返回 **409**：服务层先做可读的预检，数据库约束兜并发写入的竞态；
- `algorithm_configs.sample_id` 是唯一的外键（工作区内部引用，`ON DELETE SET NULL`）；
  配置表与工作区之间不加外键，理由同既有私有表（`workspace_id = 0` 是合法值，工作区删除走
  显式 purge 而不是数据库级联）。

### 2.4 与既有私有表的关系

引入本功能后，工作区私有表由四张变为**七张**：

```text
watchlist_items / alert_rules / alert_triggers / backtest_runs
+ screening_configs / sample_configs / algorithm_configs
```

关系约定：

- **只共享「定义」，不复制数据**。样本解析、选股求值每次实时读取全站共享的
  `daily_bars` / `factors` / `instruments`，配置行里不存行情、因子或代码清单副本；
  模型产物仍写入全局 `model_registry`（模型是参考数据，不按工作区复制 N 份）。
- **引用只在工作区内解析**。`universe_type=screening` 的 `universe_value` 是**同一工作区**的
  选股配置 id；`algorithm_configs.sample_id` 同理。拿 A 工作区的 id 换 B 工作区的
  `X-Workspace-Id` **直接访问/修改** → 404（不泄露 id 是否存在）；而把它写进 B 工作区的
  样本/算法配置（当作引用值）→ 422「引用的配置不存在」，因为这是一份内容非法的配置，
  不是一次越权读取。
- **配置随工作区生命周期**。纳入 `DATA_MODELS` 后，工作区删除的 `purge` 统计与级联清理、
  未归属数据的 `workspace claim` 都会覆盖这三张表（见 §6.3）。
- **回测历史是执行结果，配置是可引用定义**。回测请求引用 `screening_id`，运行结果仍存
  `backtest_runs.params_json` 请求快照 —— 配置之后被修改，历史记录仍保留「当时引用的是哪个
  配置 id + 哪些请求参数」；若要逐笔复现，还需要该配置或快照里的显式 `codes` 仍然可得。

## 3. 权限矩阵

角色规则完全沿用 `docs/WORKSPACE.md` §2 的三级模型（`ROLE_RANK` = owner > editor > viewer）。

| 动作 | owner | editor | viewer |
|---|---|---|---|
| `GET` 列表 / 详情（三类配置） | ✅ | ✅ | ✅ |
| `POST` / `PATCH` / `DELETE`（三类配置） | ✅ | ✅ | ❌ 403 |
| `POST /strategy/screenings/{id}/run`（运行选股，只读求值） | ✅ | ✅ | ✅ |
| `POST /strategy/samples/{id}/resolve`（解析样本，只读求值） | ✅ | ✅ | ✅ |
| `POST /strategy/algorithms/{id}/train`（训练，吃 CPU 且写模型产物） | ✅ | ✅ | ❌ 403 |
| 管理工作区成员 / 改名 / 转让 | ✅ | ❌ | ❌ |

要点：

- **viewer 可以「用」，不能「改」**。运行选股、解析样本是只读求值（不落配置、不写模型），
  刻意保持可用 —— 与 `/alerts/preview` 对 viewer 开放是同一个理由：看结果才能判断要不要申请
  编辑权限；
- **写操作 403 带 `X-Auth-Error: read_only`**，前端据此禁用写按钮并显示只读提示；
- **跨工作区一律 404**（由服务层的 `belongs_to` 归属检查保证），不用 403：403 等于确认
  「这个 id 存在，只是不给你看」，可以靠状态码差异枚举出某个工作区有多少条配置；
- **重名 409**（同工作区，`{"detail": "同名配置已存在：..."}`）：服务层先查一次给可读错误，
  数据库唯一约束兜并发写入；空名/超长/未知算法/未知超参/越界/非法 `universe_type`/引用不存在/
  日期窗口冲突等一律 422；
- **训练失败用 409**：样本标的池为空（`empty_sample`）、窗口内历史不足（`insufficient_data`）
  都是「当前数据不足以完成这次执行」，不是请求格式问题，也不写坏模型注册表；
- **`SM_DATA_ISOLATION=false` 时矩阵退化**：所有请求落 `workspace_id = 0`，`require_editor`
  恒通过，任何登录/匿名调用都能读写 —— 与引入工作区之前的行为一致。

## 4. REST API

前缀 `/api/v1`；当前工作区由请求头 `X-Workspace-Id` 解析，缺省落在调用者的 personal 工作区
（`SM_DATA_ISOLATION=true` 时）。开启 `SM_AUTH_ENABLED` 时还需 `Authorization: Bearer <token>`。

| 方法 | 路径 | 依赖（最低角色） | 成功码 | 说明 |
|---|---|---|---|---|
| `GET` | `/strategy/screenings` | `current_workspace`（viewer） | 200 | 本工作区选股配置列表（按名称、id 排序） |
| `POST` | `/strategy/screenings` | `require_editor` | 201 | 新建；重名 409 |
| `GET` | `/strategy/screenings/{id}` | `current_workspace` | 200 | 详情；跨工作区/不存在 404 |
| `PATCH` | `/strategy/screenings/{id}` | `require_editor` | 200 | 部分更新；`params` 整体替换 |
| `DELETE` | `/strategy/screenings/{id}` | `require_editor` | 204 | 删除 |
| `POST` | `/strategy/screenings/{id}/run` | `current_workspace` | 200 | 用当前因子快照求值，返回 `{trade_date, count, items[]}` |
| `GET` | `/strategy/samples` | `current_workspace` | 200 | 样本配置列表 |
| `POST` | `/strategy/samples` | `require_editor` | 201 | 新建；窗口冲突/非法类型 422 |
| `GET` | `/strategy/samples/{id}` | `current_workspace` | 200 | 详情 |
| `PATCH` | `/strategy/samples/{id}` | `require_editor` | 200 | 部分更新 |
| `DELETE` | `/strategy/samples/{id}` | `require_editor` | 204 | 删除；引用它的算法配置 `sample_id` 置空 |
| `POST` | `/strategy/samples/{id}/resolve` | `current_workspace` | 200 | 解析标的池，返回 `{codes[], count, start, end, horizon}` |
| `GET` | `/strategy/algorithms` | `current_workspace` | 200 | 算法配置列表 |
| `POST` | `/strategy/algorithms` | `require_editor` | 201 | 新建；未知 algo 422 |
| `GET` | `/strategy/algorithms/{id}` | `current_workspace` | 200 | 详情 |
| `PATCH` | `/strategy/algorithms/{id}` | `require_editor` | 200 | 部分更新 |
| `DELETE` | `/strategy/algorithms/{id}` | `require_editor` | 204 | 删除 |
| `POST` | `/strategy/algorithms/{id}/train` | `require_editor` | 200 | 同步训练，返回 `{model_id, algo, horizon, metrics, n_train, n_test}` |

错误语义统一为 `{"detail": "..."}`（由 `main.py` 的领域错误处理器翻译）；角色类错误额外带
响应头 `X-Auth-Error`。补充约定：

- `run` / `resolve` 的「没有数据」不是 HTTP 错误：选股返回 `count=0`、`trade_date=null`
  （提示先跑 `analytics`），样本解析返回空 `codes`（`count=0`）；
- `train` 在样本为空或历史不足时返回 **409**；超参/引用非法在写入配置时已 **422**，训练时
  还会再校验一次（防止库里存了旧版本配置）；
- 列表只返回当前工作区的配置；跨工作区的 id 在详情/改/删/run/resolve/train 上统一 404。

### 4.1 curl 示例

以下假设 `SM_AUTH_ENABLED=false`（本地单机默认，无需 token），并切到 2 号工作区。

**① 新建选股配置并运行**

```bash
# 创建：高分 + 低波动 + 排除 ST，按 score 倒序取前 30
curl -s -X POST http://127.0.0.1:8770/api/v1/strategy/screenings \
  -H 'Content-Type: application/json' -H 'X-Workspace-Id: 2' \
  -d '{
        "name": "高分低波",
        "description": "score >= 60 且 20 日波动 <= 0.4，排除 ST",
        "params": {"min_score": 60, "max_vol": 0.4, "exclude_st": true,
                   "order_by": "score", "direction": "desc", "limit": 30}
      }'

# 运行求值（viewer 也可调用）
curl -s -X POST http://127.0.0.1:8770/api/v1/strategy/screenings/3/run \
  -H 'X-Workspace-Id: 2'
```

**② 新建样本配置并解析标的池**

```bash
# universe_type=screening：引用同工作区 3 号选股配置，最近 730 个自然日
curl -s -X POST http://127.0.0.1:8770/api/v1/strategy/samples \
  -H 'Content-Type: application/json' -H 'X-Workspace-Id: 2' \
  -d '{
        "name": "高分低波两年样本",
        "universe_type": "screening", "universe_value": "3",
        "lookback_days": 730, "horizon": 5, "min_bars": 60
      }'

curl -s -X POST http://127.0.0.1:8770/api/v1/strategy/samples/5/resolve \
  -H 'X-Workspace-Id: 2'
```

**③ 新建算法配置并训练**

```bash
curl -s -X POST http://127.0.0.1:8770/api/v1/strategy/algorithms \
  -H 'Content-Type: application/json' -H 'X-Workspace-Id: 2' \
  -d '{
        "name": "GBDT-5日",
        "algo": "hist_gbdt", "sample_id": 5, "horizon": 5, "test_fraction": 0.2,
        "params": {"max_iter": 300, "learning_rate": 0.05, "max_depth": 4, "min_samples_leaf": 50}
      }'

# 同步训练，返回 model_id 与样本外指标
curl -s -X POST http://127.0.0.1:8770/api/v1/strategy/algorithms/7/train \
  -H 'X-Workspace-Id: 2'
```

**④ 回测直接引用选股配置**（`codes` 留空时用配置解析标的池）

```bash
curl -s -X POST http://127.0.0.1:8770/api/v1/backtest/run \
  -H 'Content-Type: application/json' -H 'X-Workspace-Id: 2' \
  -d '{"strategy": "score_top_n", "screening_id": 3,
       "start": "2024-01-01", "end": "2025-12-31"}'
```

## 5. 与既有流程的集成

### 5.1 回测：`BacktestRequest.screening_id`

- 请求体新增可选字段 `screening_id`。**显式 `codes` 优先**：`codes` 非空时忽略
  `screening_id`，避免「选了配置又手填代码」时默默用错其中一个；
- `codes` 为空且给了 `screening_id` 时，按该选股配置（同一工作区、最新因子日）求值，取结果
  代码作为标的池；配置不存在或属于其他工作区 → **404**；求值结果为空（一只都没命中）→
  **409**，而不是悄悄回退默认宇宙——否则用户会以为在测自己的策略，实际测的是别的东西；
- 两者都为空时退回「默认宇宙」：隔离开启时取本工作区自选并集，关闭时取全站自选并集
  （`_default_universe(workspace_id=None)`）；
- 回测仍要求 **editor 及以上**（吃 CPU、写共享历史）；viewer 想预览筛选结果用
  `POST /strategy/screenings/{id}/run`，不跑回测；
- 落库的 `backtest_runs.params_json` 保存完整请求快照（含 `screening_id`），因此历史回测
  可复盘「当时引用的是哪套选股口径」；`universe` 列在未显式传 `codes` 时记为 `default`，
  实际标的池以快照里的 `screening_id` 重新求值为准。

### 5.2 算法训练

`POST /strategy/algorithms/{id}/train` 把配置翻译成一次真实训练：

1. **解析样本**：`sample_id` 非空时按 §5.3 解析该样本配置；为空时用调度器默认宇宙 +
   全量历史 + `min_bars=60`；
2. 按代码加载**前复权日线**（共享行情，不复制）；
3. 按样本窗口裁剪，并剔除窗口内历史不足 `min_bars` 根 bar 的标的；
4. `build_dataset(bars, horizon=配置.horizon)` → 严格时序切分（含标签净化 purge）；
5. 调用 `train_model(dataset, algo=配置.algo, test_fraction=配置.test_fraction,
   algo_params=配置.params_json 归一化结果)`；
6. `persist=True`，产物写入**全局** `model_registry`（模型是共享参考数据，不按工作区隔离），
   返回 `model_id / metrics / n_train / n_test`。

训练是**同步**接口、CPU 密集，因此要求 editor 及以上；不进入调度器，也不自动激活模型 ——
训练完成后预测链路仍按「最新已激活模型」工作。算法配置不会自动触发预测或定时重训
（见 §9 未做项）。

失败语义：解析出的标的池为空 → 409 `empty_sample`；窗口内每个标的都凑不够 `min_bars` 根
日线 → 409 `insufficient_data`。两种情况都不会写 `model_registry`。

### 5.3 样本解析

`POST /strategy/samples/{id}/resolve` 与训练共用同一套解析逻辑，四种 `universe_type`：

| `universe_type` | `universe_value` 解释 | 标的池来源 |
|---|---|---|
| `watchlist` | 自选清单名（空 = `default`） | 该样本所属工作区的 `watchlist_items`（隔离关闭时配置本身在 `workspace_id=0`，读的就是共享桶） |
| `screening` | 选股配置 id（字符串） | 求值同工作区的该选股配置，取结果代码；写入时校验引用存在，配置被删除后再解析 → 422 |
| `codes` | 逗号/分号/空白/中文逗号顿号分隔的显式代码 | 逐只 `normalize()` 规范化、去重；非法代码 422（不静默丢弃） |
| `default` | 空 | `_default_universe(workspace_id)`：**有行情的自选并集 → 沪深 300 成分 → 全部有行情的标的** 的兜底链；隔离开启时按本工作区自选取，关闭时取全站自选并集 |

日期窗口按 §2.2 的互斥规则解析：显式区间优先；只给 `lookback_days` 时以库中**最新交易日**
（`MAX(daily_bars.trade_date)`，不是「今天」）为终点向前推 N 个自然日；都不给用全量历史。
`resolve` 返回候选池与窗口本身（编码为 `codes / count / start / end / horizon`），**不**按
`min_bars` 过滤 —— 门槛在训练阶段结合真实 bar 数生效。viewer 可调用（只读）。

## 6. 迁移、开关与数据治理

### 6.1 迁移 `0004_strategy_configs`

- 迁移链：`0001_baseline → 0002_owner_columns → 0003_workspaces → 0004_strategy_configs`；
- `down_revision = "0003_workspaces"`，只建三张表与索引，**不回填数据**（配置是新功能，没有
  历史行可搬）；建表顺序 `screening → sample → algorithm`（`algorithm_configs.sample_id` 有外键）；
- **幂等**：每个 `create_table` 前先查 `_has_table()`。原因是两条通道都可能先建表 ——
  全新库 `init_db()` 先 `create_all`（三张表已由 ORM 元数据建好）再 bridge 到 Alembic，
  迁移必须跳过；存量库则可能由 `create_all` 或 `alembic upgrade` 任一先补齐；
- `downgrade` 直接删三张表：配置是纯用户数据，没有下游表引用；行情、因子、模型产物不受影响，
  但配置内容会随回退丢失；
- 存量库纳管仍走既有约定：启动时 `ensure_migrated()` 自动 `upgrade head`（或手动
  `stockmaster db upgrade`），不需要重放历史迁移；`SM_DB_AUTO_MIGRATE=false` 时要求先显式升级。

### 6.2 `SM_DATA_ISOLATION` 开关语义

| 开关 | 归属 | 可见范围 | 角色 |
|---|---|---|---|
| `false`（默认） | 全部配置写 `workspace_id = 0` | 所有人看到同一份（`0` 号桶） | `require_editor` 恒通过；与引入工作区前一致 |
| `true` | 按请求头 `X-Workspace-Id` 归属，缺省 personal 工作区 | 只看到当前工作区；跨工作区 404 | owner/editor/viewer 三级生效 |

- 列与迁移**始终存在**，切回 `false` 只改环境变量、不需要回滚数据库；
- `false` 时「工作区内重名」退化为「`0` 号桶内唯一」，等价于旧语义的全站唯一；
- 从 `false` 切到 `true` 前，历史配置留在 `workspace_id = 0`，用 `workspace claim` 移交（§6.3）。

### 6.3 与 `data_counts` / `purge` / `claim_unassigned` 的集成

三张表纳入 `src/stockmaster/plugins/workspaces/services/service.py` 的 `DATA_MODELS`：

- **`data_counts()`**：工作区页会显示「选股/样本/算法配置各 N 条」，删除工作区前可确认；
- **`delete_workspace(..., purge=True)`**：按表清理这三类配置，返回值计入删除统计；
- **`claim_unassigned()`**：`workspace claim --id N` 把 `workspace_id = 0` 的配置搬进目标工作区。
  由于唯一键是 `(workspace_id, name)`，目标工作区若已有同名配置，旧的无主副本被删除、工作区
  自己那份保留，冲突条数计入 `duplicates_skipped`，并在输出中如实报告 —— 与自选清单同码冲突
  的处理方式保持一致；
- **CLI 无新增命令**：创建工作区、认领数据仍复用既有 `stockmaster workspace ...` 命令；
  配置的增删改查走 API/前端。

推荐开启顺序：升级代码（迁移自动建三张空表）→ `workspace claim` 移交未归属配置 →
置 `SM_DATA_ISOLATION=true` 重启 → 在「策略」页创建/整理配置。

## 7. 前端操作路径

- **入口**：顶栏「策略」→ 路由 `/strategy`，与「分析/预测/回测」同级的独立页面；
- **三个 tab**：`选股配置` / `样本配置` / `算法配置`，每个 tab 是「列表 + 新建/编辑 + 删除」：
  - 选股：编辑分数/波动/成交额门槛、板块、逐条条件（按 `field op value` 的文本行维护，提交前
    解析，无法解析的行给出带行号的提示、不静默丢弃）、排序与条数；点「运行」展示结果表
    （`trade_date / count / items[]`），并明确提示「运行是只读求值、不修改数据」；
  - 样本：选择 `universe_type`（自选清单 / 选股配置 / 显式代码 / 默认宇宙）、清单名/id/代码、
    日期窗口或回看天数；点「解析」显示标的数、区间与 `horizon`；
  - 算法：选择算法与绑定样本、填超参/周期/测试集占比；点「训练」展示 `model_id` 与
    样本外指标（AUC/IC/准确率、训练/测试条数），并提示会写入模型列表；
- **只读态**：viewer 下所有写操作（新建/保存/删除/训练）按钮禁用，并显示
  「当前工作区为只读角色（viewer）：配置不可新建、修改、删除或训练，仅可查看列表、运行选股与
  解析样本」；运行选股与解析样本保持可用（`canWriteWorkspace()` 判定）；
- **工作区切换**：请求自动携带 `X-Workspace-Id`；`<Routes key={currentId}>` 整树重挂载 +
  React Query 重取，列表立即切到新工作区的配置；
- **草稿持久化**：zustand view store（`localStorage` 键 `stockmaster.view`）的 `strategy` 切片
  保存当前 tab 与三个表单草稿，切页面/刷新不丢；草稿是「未提交的表单」，已保存的配置是后端
  资源，两者刻意分开（草稿不会自动写库）；
- **执行结果不持久化**：运行选股 / 解析样本 / 训练的结果是「执行一次就能再生」的派生数据，
  存在组件本地 state，切走 tab 即丢弃，避免把易陈旧的结果写进本地存储；
- **列表与刷新**：React Query 的 key 形如 `["strategy", "screenings", workspace?.id]`（样本 /
  算法同理）；写操作成功后 invalidate 对应配置类型，列表与详情同步刷新；列表展示名称、
  描述、更新时间与运行/训练入口；
- **编辑语义**：编辑已有项时把整行数据填回表单，PATCH 仍提交完整 input —— 后端 `params`
  是整体替换语义，只提交改动的字段会让未改字段被重置。

## 8. 测试矩阵

- `tests/test_strategy_configs.py`（新增）：
  1. 三类配置 CRUD（创建 201、列表、详情、PATCH、DELETE 204）；
  2. 角色矩阵：viewer 对全部写端点 403 且 `X-Auth-Error=read_only`；viewer 可 GET、可运行选股、
     可解析样本；editor/owner 可写；
  3. 跨工作区交叉访问一律 404；重名 409；空名/未知 algo/非法 `universe_type`/日期窗口冲突 422；
  4. 选股求值：种子因子数据下 `min_score / conditions / boards / limit / 排序` 正确；
  5. 样本解析：四种 `universe_type` 与日期窗口；`min_bars` 在训练阶段按真实 bar 数过滤；
  6. 算法训练：合成 bar + 临时库以 `logistic` 训练成功，返回 `model_id/metrics`；
  7. 数据治理：三张表计入工作区 `purge` / `data_counts`，`claim_unassigned` 能搬运且同名冲突
     计入 `duplicates_skipped`；
- 既有 `test_workspace.py` / `test_isolation.py` 的角色与隔离约定同样适用于新端点；
- 本功能不引入网络契约测试；训练测试使用合成数据，不依赖真实行情。

## 9. 未做的（后续 PR）

- **定时任务引用策略**：调度器按算法配置定期重训、按选股配置定期生成候选池；
  当前训练与选股都是手动同步调用；
- **按策略生成观察池**：把选股运行结果物化成自选清单/预警 `scope`，实现「选股 → 观察 → 预警」
  的自动衔接；
- **跨工作区复制 / 导入导出**：配置的 JSON/YAML 导入导出与工作区间复制（当前只能手工重建）；
- **配额与统计**：每个工作区的配置数量上限、被运行/训练次数统计（工作区级配额仍是
  `docs/WORKSPACE.md` §11 的未做项）；
- **配置版本历史与审计**：谁在什么时候改了哪些参数、可回滚（当前只有 `created_by`/`updated_at`）；
- **回测/预测全链路绑定**：回测目前只引用选股配置，预测与回测历史尚未记录「用的是哪个
  算法配置」；算法训练完成后也不会自动激活模型；
- **按工作区通知通道**：webhook 仍是全站一个（与 `docs/WORKSPACE.md` §11 相同）。
