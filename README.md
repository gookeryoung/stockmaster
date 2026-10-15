# StockMaster · A股日线量化分析工作台

基于 **Python (FastAPI) + React (TypeScript/Vite/ECharts)** 的单机量化研究工作台，
闭环覆盖 **数据获取 → 分析 → 预测 → 预警**，并内置 **回测验证**。

> ⚠️ **免责声明**：本项目为量化研究工具，所有数据、指标、预测与回测结果**仅供研究参考，不构成任何投资建议**。
> 历史表现不代表未来收益，据此交易的风险由使用者自行承担。

---

## 1. 功能概览

| 模块 | 能力 |
|---|---|
| **数据获取** | A股日线（前复权/不复权）、指数、交易日历、证券主数据、盘中快照；多源容错与熔断降级；增量同步与断点续传 |
| **分析** | 23 列技术指标（MA/EMA/MACD/RSI/KDJ/BOLL/ATR/OBV/量比）+ 14 项横截面因子 + 综合评分与分位排名 + 多因子选股 + 相关性分析 |
| **预测** | 梯度提升 / 随机森林 / 逻辑回归；严格时序切分（含标签净化 purge）与滚动走查；输出上涨概率、AUC/IC 及**随机基线对照** |
| **回测** | 自研日线撮合引擎：T+1、涨跌停不可成交、停牌、佣金（含最低 5 元）、印花税、过户费、滑点 |
| **预警** | 13 类规则（价格/涨跌幅/量比/换手/均线/MACD/RSI/KDJ/BOLL/新高/回撤/预测/因子分）；**去重幂等**；WebSocket/Webhook 推送 |
| **工作台** | 7 个页面：总览、行情（K线+指标）、分析、预测、回测、预警、设置 |

---

## 2. 快速开始

### 2.1 环境要求
- Python **3.13+**（实测 3.13.13）
- Node.js **20+**（实测 v25.2.1）与 pnpm（实测 12.4.2）
- 无需 Docker、无需 Redis、无需任何付费数据 Token

### 2.2 安装

**方式一：Make（推荐，等价于下方手动步骤）**

```bash
make sync            # 后端依赖（uv sync --extra dev）
make frontend-sync   # 前端依赖（pnpm install）
make frontend-build  # 构建前端
make init            # 初始化数据库
```

**方式二：手动**

```powershell
# 1) 后端
uv sync --extra dev

# 2) 前端
cd frontend
pnpm install
pnpm build      # 产物 frontend/dist 由后端托管
cd ..
```

### 2.3 一键启动

```powershell
.\scripts\run.ps1
# 打开 http://127.0.0.1:8770
```

### 2.4 首次数据初始化（重要）

打开页面后进入 **设置** 页，按顺序点击任务；或直接用 CLI：

```powershell
.venv\Scripts\python.exe -m app.cli --version

# 核心池（约几百只，几分钟内可用）
.venv\Scripts\python.exe -m app.cli sync-calendar
.venv\Scripts\python.exe -m app.cli sync-instruments
.venv\Scripts\python.exe -m app.cli sync-daily

# 指标与因子
.venv\Scripts\python.exe -m app.cli analytics

# 训练与预测
.venv\Scripts\python.exe -m app.cli train --algo hist_gbdt --horizon 1
.venv\Scripts\python.exe -m app.cli predict
```

> **全市场回补**（耗时较长，可中断续传）：
> ```powershell
> .venv\Scripts\python.exe -m app.cli sync-daily --full
> ```
> 实测：8 只标的 × 2.8 年约 105 秒。全市场 5500+ 只建议分批进行，命令支持断点续传。

### 2.5 开发模式（前端热更新）

```powershell
# 终端 1：后端
.venv\Scripts\python.exe -m app.cli serve --reload

# 终端 2：前端（Vite 代理 /api 到 8770）
cd frontend; pnpm dev     # http://127.0.0.1:5173
```

---

## 3. 数据源策略（实测结论）

本项目的数据源选择基于**实机验证**，而非文档假设：

| 数据源 | 状态 | 用途 |
|---|---|---|
| **baostock** | ✅ 主源 | 日线（源端直接给出前/后/不复权）、交易日历、证券主数据、指数 |
| **腾讯** | ✅ 备源 1 | 日线（快，1.2s/只） |
| **新浪** | ✅ 备源 2 | 日线、全市场快照（5575 只 / 12s） |
| **东方财富** | ❌ **不可用** | 本机 Python 侧连接 100% 被重置（已排除瞬时限流/UA/TLS 指纹，`curl_cffi` impersonate 亦失败） |

**因此 AkShare 的全部 `*_em` 接口（`stock_zh_a_hist`、`stock_zh_a_spot_em`、`index_zh_a_hist` 等）在本环境不可依赖。**

### 3.1 关键修复：baostock 挂死问题
`baostock/util/socketutil.py` 创建 socket 时**未设置 timeout**，`recv()` 无界阻塞，异常又被 `print` 吞掉返回 `None`，导致查询**挂死数分钟**。

本项目在登录后强制设置 socket 超时（见 `BaostockProvider._apply_socket_timeout`），并叠加**硬超时 + 熔断降级**：

| 调用 | 修复前 | 修复后 |
|---|---|---|
| 交易日历 | 挂死 | 0.07s |
| 个股日线 | 挂死 | 0.05s |
| 证券主数据（baostock 超时） | 挂死 | 30s 后自动降级腾讯，43s 完成 5575 只 |

### 3.2 复权正确性
前复权价会因**新的除权除息**而整段变化。若直接把新数据 upsert 到旧序列，会在边界产生虚假跳空，污染全部指标与回测。

本项目的处理：**不复权价为不可变基准**，`adjust_factors` 记录除权点；检测到新增除权日后**整体重建**该股前复权序列，并用连续性校验（跳变 > 25% 视为异常）守护。

---

## 4. 项目结构

```
stockmaster/
├─ Makefile            # 快捷命令（make help 查看全部）
├─ pyproject.toml      # 依赖、pytest/coverage/bumpversion 配置
├─ ruff.toml           # lint 规则（与 cndb 口径一致）
├─ .pre-commit-config.yaml
├─ .gitattributes      # 行尾规范化
├─ backend/app/
│  ├─ providers/    # 数据源抽象 + baostock/腾讯/新浪 + 熔断注册表
│  ├─ sync/         # 仓储、增量同步、断点续传、复权重建
│  ├─ indicators/   # 23 列技术指标
│  ├─ factors/      # 横截面因子 + 标准化 + 综合评分
│  ├─ model/        # 数据集、时序切分、模型、评估、训练
│  ├─ backtest/     # 撮合引擎 + 信号生成
│  ├─ alert/        # 规则引擎 + 去重 + 推送通道
│  ├─ jobs/         # 流水线任务 + APScheduler
│  ├─ api/v1/       # FastAPI 路由
│  └─ cli.py        # 命令行（全市场回补等）
├─ frontend/src/    # React + ECharts 工作台
├─ backend/tests/   # 374 项离线测试 + 12 项网络契约测试
└─ scripts/         # 启动、验收脚本
```

---

## 5. 方法论要点（为什么结果可信）

1. **无未来函数**：指标/因子在 `t` 时刻只用 `≤t` 数据，并有专门测试断言「截断未来数据后历史值不变」。
2. **标签净化（purge）**：训练集尾部 `horizon` 根 bar 的标签会用到期后价格，全部剔除，防止测试期信息泄漏进训练集。
3. **基线对照**：所有模型指标都附随机基线（AUC 0.5 / 多数类准确率），避免把「看起来不错」误读为「有效信号」。
4. **严格信号时序**：`t` 日信号在 `t+1` 开盘成交，由引擎强制 `shift`，不依赖调用方自觉。
5. **去重幂等**：预警以 `(rule_id, dedup_key)` 唯一约束去重，重复扫描不重复提醒。

### 5.1 关于当前模型效果的诚实说明
小样本（8 只标的）实测 **样本外 AUC ≈ 0.48，IC ≈ −0.02**，即**未跑赢随机基线**。
这是预期结果：横截面模型需要足够宽的股票池才能学到截面差异。要获得有意义的模型，
请先执行 `sync-daily --full` 回补全市场，再用 `train` 重新训练。

---

## 6. 测试与验收

```bash
make test            # 离线测试（并行；网络测试自动跳过）
make cov             # 测试 + 覆盖率报告（当前 76%，门槛 75%）
make test-network    # 网络契约测试（验证真实数据源与跨源一致性）
make acceptance      # 端到端实盘验收（真实数据走完整链路）
make lint            # ruff check + format 校验
make check-fast      # 轻量静态门禁（lint + 前端类型检查，不含测试）
make check           # 全套门禁（lint + 前端类型检查 + 测试 + 覆盖率）
```

等价的手动命令：

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q        # 离线
.venv\Scripts\python.exe -m pytest backend/tests -q -m network
.venv\Scripts\python.exe -u scripts\acceptance_live.py
cd frontend; pnpm exec tsc --noEmit; pnpm build
```

当前状态：**374 项离线测试通过（12 项网络测试默认跳过）**，12 项网络契约测试实盘通过，
覆盖率 **76.25%**，实盘端到端验收全部通过。

### 6.1 全部 Make 命令

运行 `make help` 可随时查看。常用命令：

| 命令 | 说明 |
|---|---|
| `make sync` / `make init` | 安装后端依赖 / 初始化数据库 |
| `make frontend-sync` / `make frontend-build` | 安装前端依赖 / 构建前端 |
| `make serve` | 启动服务（默认 `127.0.0.1:8770`，可用 `DEV_PORT=` 覆盖） |
| `make dev` | 显示开发模式两终端启动方式 |
| `make stats` | 数据库概况（证券数、日线行数、最新行情日、模型数） |
| `make acceptance` | 端到端实盘验收 |
| `make clean` | 清理构建产物与缓存 |
| `make bump` | 版本号 bump（`bump-my-version`，同步 pyproject 与 `__init__`） |

### 6.2 Git 钩子（可选）

```bash
uv run --with prek prek install --force   # 安装钩子
uv run --with prek prek run --all-files   # 手动全量运行
```

钩子使用 ruff **0.16.8**，与 `pyproject.toml` 的 `ruff==` 及 `.pre-commit-config.yaml` 的 `rev` 三处保持一致，
避免同一提交在本地与 CI 得到不同结果。

---

## 7. 配置

复制 `.env.example` 为 `.env` 后按需修改（全部以 `SM_` 为前缀）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SM_PORT` | 8770 | 服务端口 |
| `SM_PROVIDER_ORDER` | baostock,tencent,sina | 数据源优先级 |
| `SM_PROVIDER_TIMEOUT` | 30 | 单请求超时（秒） |
| `SM_SYNC_WORKERS` | 4 | 并发取数线程 |
| `SM_PREDICT_HORIZONS` | 1,5,20 | 预测周期 |
| `SM_SCHEDULER_ENABLED` | true | 是否启用定时任务 |
| `SM_ALERT_WEBHOOK_URL` | 空 | 预警推送 Webhook（留空则不外部推送） |

### 定时任务（Asia/Shanghai，仅交易日）
`15:20` 日历 → `15:35` 日线 → `16:05` 指数 → `16:30` 指标因子 → `16:45` 预测 → `17:00` 预警 → 盘中每 60s 快照 → 周六 `02:00` 重训。

---

## 8. 已知限制

- **单机单用户**，默认仅监听 `127.0.0.1`，无登录与权限体系。
- 仅日线级；不含分钟级、Level-2、实盘下单、融资融券/做空。
- 后复权（hfq）为**按需派生**，不落库（API 会明确返回 501 而非静默降级）。
- 全市场指标/因子历史未落库（只存最新一行）；回测时按需从日线即时重算，以换取更小的库体积。
- 数据源为公开免费接口，存在限流与偶发不可用；系统会降级但**不保证每次成功**。

---

## 9. 许可与数据合规

数据来源于公开接口，请遵守各数据提供方的服务条款，仅用于个人研究。
本项目不内置任何付费数据 Token。
