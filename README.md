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
| **工作台** | 8 个页面：总览、行情（K线+指标）、分析、预测、回测、预警、设置、用户 |
| **账号** | 用户名/邮箱 + 密码、微信扫码登录（开放平台/公众号）、账号绑定与解绑、角色（管理员/普通用户）、启用/禁用、审计留痕；可选强制登录 |

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

打开页面后进入 **设置** 页，按顺序点击任务；或直接用 CLI（经 `uv run` 调用，无需手动激活虚拟环境）：

```bash
uv run stockmaster sync-calendar      # 交易日历
uv run stockmaster sync-instruments   # 证券主数据
uv run stockmaster sync-daily         # 核心池日线（几分钟内可用）
uv run stockmaster analytics          # 指标与因子
uv run stockmaster train              # 训练模型（默认梯度提升，1 日周期）
uv run stockmaster predict            # 批量预测
```

不带 `sync-xxx` 前缀的等价写法也可用 `make`：

```bash
make init           # 建库
make stats          # 查看数据概况
make doctor         # 环境自检（依赖/编码/路径）
```

> **全市场回补**（耗时较长，可中断续传）：
> ```bash
> uv run stockmaster sync-daily --full
> ```
> 实测：8 只标的 × 2.8 年约 105 秒。全市场 5500+ 只建议分批进行，命令支持断点续传。

### 2.5 Docker 部署（生产形态）

单容器形态：镜像内已含前端构建产物，FastAPI 同源托管 SPA + API，无需额外 nginx。
数据（SQLite 库 + 模型产物）落在命名卷 `stockmaster-data`，换版本不动卷。

```bash
# 服务器上（首次）
git clone <仓库地址> /opt/stockmaster && cd /opt/stockmaster
cp deploy/.env.prod.example .env && vim .env      # 改 SM_BIND 端口等
./deploy/deploy.sh                                # 本地构建镜像 → 备份 → 重建 → 健康探测
```

`deploy.sh` 的关键行为：

| 行为 | 说明 |
|------|------|
| **服务器本地构建** | 镜像不过网，不依赖镜像仓库；基础镜像走 `mirror.ccs.tencentyun.com` 内网源 |
| **预备份** | 重建前把数据卷打包到 `backups/pre-deploy-<版本>-<时间戳>.tar.gz`，仅保留最近 5 份 |
| **健康探测** | 轮询 `/api/v1/health` 最长 180s，失败自动回滚到上一个镜像并打印容器日志 |

容器内运维命令（`app.cli` 与本地 CLI 完全等价）：

```bash
docker compose --env-file .env -f docker-compose.prod.yml exec app python -m app.cli stats
docker compose --env-file .env -f docker-compose.prod.yml exec app python -m app.cli sync-calendar
docker compose --env-file .env -f docker-compose.prod.yml exec app python -m app.cli sync-daily
```

相关文件：

| 文件 | 作用 |
|---|---|
| `docker-compose.prod.yml` | 生产编排（单容器 / 数据卷 / 日志轮转 / 健康检查） |
| `deploy/Dockerfile.prod` | 生产镜像（多阶段：node 构建前端 → python 运行时） |
| `deploy/deploy.sh` | 服务器端部署（本地构建 / 预备份 / 健康探测 / 失败回滚） |
| `deploy/.env.prod.example` | 生产环境变量模板 |

完整部署链路、端口与防火墙、数据初始化：见 **[docs/DEPLOY_TENCENT.md](docs/DEPLOY_TENCENT.md)**。

### 2.6 开发模式

```bash
make dev            # 一条命令在当前控制台启动后端(8770) + 前端 Vite(5173)，Ctrl+C 一并停止
make dev-stop       # 清理遗留的开发进程并释放端口
```

- 前端界面： http://127.0.0.1:5173 （Vite 已把 `/api` 代理到后端 8770）
- API 文档： http://127.0.0.1:8770/docs
- Vite 以 `--host 127.0.0.1 --strictPort` 启动：**端口被占用会直接报错**，而不是
  悄悄换到 5174——否则脚本打印的 URL 会与实际不符。
- 也可分别启动：`make backend`（仅后端）与 `make frontend-dev`（仅前端）。

> 前提：`make` 可用（Windows 上建议安装 [Gow](https://github.com/bmatcuk/gow)
> 或 Git Bash 自带的 make）。若没有 `make`，直接用上面的 `uv run stockmaster ...`
> 与 `.\scripts\run.ps1` 即可，功能完全一致。

前端热更新：`make dev` 中的 Vite 已启用 HMR，改动 `frontend/src` 会即时生效。
后端如需热重载，用 `uv run stockmaster serve --reload`。

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

### 3.1 各代码段可访问性（逐段实测）

对每个代码段各取代表性标的一次性实测（区间 2026-09-01 ~ 2026-10-15），
逐数据源直连验证：

| 代码段 | 说明 | baostock | 腾讯 | 新浪 | 可用来源 |
|---|---|---|---|---|---|
| `600/601/603/605` | 沪主板 | ✅ 0.1–2.0s | ✅ | ✅ | 全部 |
| `688/689` | 科创板 | ✅ | ✅ | ✅ | 全部 |
| `000/001/002/003` | 深主板 | ✅ | ✅ | ✅ | 全部 |
| `300/301/302` | 创业板 | ✅ | ✅ | ✅ | 全部 |
| `900xxx` | 沪B股 | ❌ 空 | — | ✅ | 仅新浪 |
| `200xxx` | 深B股 | ❌ 空 | — | ✅ | 仅新浪 |
| `920xxx` | 北交所（新号段） | ❌ 报错 | ❌ `KeyError: 'day'` | ✅ | **仅新浪** |
| `43x/83x/87x/88x` | 北交所（旧号段） | ❌ 报错 | ❌ | ❌ `No value to decode` | **无来源** |
| `000300/3990xx` 等 | 指数（sh/sz） | ✅ | ✅ 6–13s | ✅ 0.2s | 全部 |

要点：

- **普通沪深股票全代码段覆盖良好**，首选 baostock。
- **指数必须走 `fetch_index_daily`**：备源的股票接口按个股解析，指数代码会失败。
- **北交所旧号段目前无任何来源**，`920xxx` 仅新浪可获取日线。
- 各 provider 通过 `supports()` 声明边界，不受支持的代码会被**跳过而不计入失败**，
  避免把健康的数据源误判为故障（见 `test_code_coverage.py`）。

> 该矩阵由实测得出，会随上游接口变化；如需复核可重新运行探测脚本或参考
> `backend/tests/test_code_coverage.py` 中的断言。

### 3.2 关键修复：baostock 挂死问题
`baostock/util/socketutil.py` 创建 socket 时**未设置 timeout**，`recv()` 无界阻塞，异常又被 `print` 吞掉返回 `None`，导致查询**挂死数分钟**。

本项目在登录后强制设置 socket 超时（见 `BaostockProvider._apply_socket_timeout`），并叠加**硬超时 + 熔断降级**：

| 调用 | 修复前 | 修复后 |
|---|---|---|
| 交易日历 | 挂死 | 0.07s |
| 个股日线 | 挂死 | 0.05s |
| 证券主数据（baostock 超时） | 挂死 | 30s 后自动降级腾讯，43s 完成 5575 只 |

### 3.3 复权正确性
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
│  └─ cli.py        # 命令行（回补/体检/训练等）
├─ .github/workflows/ci.yml  # CI（后端+前端）与 tag 触发的 Release
├─ frontend/src/    # React + ECharts 工作台
├─ backend/tests/   # 448 项测试（含 12 项网络契约测试，默认跳过）
└─ scripts/         # 启动、开发、清理、验收脚本
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

当前状态：**448 项测试通过（12 项网络契约测试默认跳过）**，网络契约测试可实盘通过，
覆盖率 **76.25%**，实盘端到端验收全部通过。

### 6.1 全部 Make 命令

运行 `make help` 可随时查看（帮助文本会按控制台代码页选择编码，中文控制台与 UTF-8 终端都不乱码）。

| 命令 | 说明 |
|---|---|
| `make sync` / `make init` | 安装后端依赖 / 初始化数据库 |
| `make frontend-sync` / `make frontend-build` | 安装前端依赖 / 构建前端 |
| `make backend` (`serve`) | 启动服务（默认 `127.0.0.1:8770`，可用 `DEV_PORT=` 覆盖） |
| `make frontend-dev` | 仅启动前端 Vite（`--host 127.0.0.1 --strictPort`） |
| `make dev` | **启动开发环境**（后端 + 前端，当前控制台内前台运行，Ctrl+C 一并停止） |
| `make dev-stop` | 清理遗留的开发服务进程并释放端口 |
| `make doctor` (`typecheck`) | 环境自检：依赖、控制台编码、路径、数据源配置 |
| `make stats` | 数据库概况（证券数、日线行数、最新行情日、模型数） |
| `make data-quality` | **数据质量体检**：覆盖率、缺口、陈旧、异常、复权一致性 |
| `make test` / `make cov` / `make test-network` | 离线测试 / 覆盖率 / 网络契约测试 |
| `make acceptance` | 端到端实盘验收（真实数据源，需联网） |
| `make lint` / `make format` | ruff 检查 / 自动格式化 |
| `make check-fast` / `make check` | 轻量静态门禁 / 全套门禁 |
| `make clean` | 清理构建产物与缓存（`.venv` / `data` / `node_modules` 始终保留） |
| `make pub` | 上传 `dist/*` 到 PyPI（需先 `make build` 并配置 PyPI 凭证） |
| `make push` | 推送当前分支到所有远程仓库（含 tags） |
| `make bump` | 版本号 bump（`bump-my-version`，同步 pyproject 与 `__init__`） |

### 6.2 数据质量体检

数据问题大多是**静默**的：某只标的停更、前复权序列停留在旧基准、历史没抓全，
都不会报错，只会让指标、因子、预测和回测悄悄算错。`data-quality` 把这些显式化。

```bash
make data-quality                                            # 全市场覆盖率 + 抽样逐只检查
uv run stockmaster data-quality --codes 600519.SH,000001.SZ  # 指定标的
uv run stockmaster data-quality --min-usable-share 0.5       # 作为门禁（低于则退出码 1）
```

检出项：`missing_history`、`missing_qfq`、`orphan_qfq`、`short_history`、
`sparse`、`gap`、`stale`、`ohlc_invalid`、`extreme_return`、`qfq_discontinuity`。

报告分两层：

- **coverage（全市场）** —— 可用/未同步/滞后数量与占比。这是"我到底有多少数据"的
  直接答案，且**不受逐只扫描上限影响**。
- **逐只 findings** —— 带具体日期与数值，每条结论都可复核。

也可通过 `GET /api/v1/system/data-quality` 获取（`only_problems=true` 便于轮询）。

### 6.3 中文乱码说明（Windows）

中文 Windows 控制台默认代码页为 **936(GBK)**，而现代终端（Windows Terminal / VS Code）
多为 **65001(UTF-8)**。同一段字节在其中一边必然乱码。

**本项目的做法：输出统一为 UTF-8，不做运行时探测。**

原因是一次实测发现"按控制台代码页输出"并不可靠 —— 本机三个信号互相矛盾：

| 信号 | 取值 |
|---|---|
| `chcp` | 936 |
| `GetConsoleOutputCP()` | 65001 ← 与实际不符 |
| `sys.stdout.encoding` | gbk |

两种探测策略各在一台机器上正确、在另一台上错误，导致中文时好时坏。因此改为固定
UTF-8（这也是现代终端/编辑器/CI 日志的通行假设），行为在任何机器上都一致：

- 需要例外时用 `SM_CONSOLE_ENCODING` 覆盖（例如仅支持 GBK 的旧控制台）；
- `make doctor` 会打印全部信号（`编码诊断` 一行），便于排查；
- `Makefile` **不使用内联 Python** 输出中文（内联 `python -c` 是新进程，编码不可控），
  改为调用 `stockmaster` CLI 与 `scripts/*.py`；
- 回归保护见 `backend/tests/test_console_bytes.py`：直接断言 CLI 子进程的
  **原始字节是合法 UTF-8**（仅断言编码名无法发现问题）。

### 6.4 CI / CD

`.github/workflows/ci.yml` 在 push / PR 时运行，tag 触发发布：

| Job | 内容 |
|---|---|
| `backend` | `uv sync --frozen` → ruff check + format → `pytest -m "not network"`（覆盖率门槛 75%）→ `uv build` |
| `frontend` | `pnpm install --frozen-lockfile` → `tsc --noEmit` → `vite build` |
| `release` | 仅 `v*` tag：汇总 wheel/sdist + 前端 zip + `SHA256SUMS.txt`，创建 GitHub Release |

设计取舍：

- **网络契约测试不进 CI**。它们依赖公开数据源（baostock/腾讯/新浪），在 CI 上会带来
  不确定性；需要时用 `make test-network` 本地手动运行。
- **发布只用仓库自带的 `GITHUB_TOKEN`**，`permissions: contents: write` 仅授予
  release job，不依赖任何外部密钥。
- 版本号由 `make bump` 维护（同步 `pyproject.toml` 与 `app/__init__.py`），
  打出的 tag 形如 `v0.1.1`，即触发发布。

### 6.5 Git 钩子（可选）

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
| `SM_AUTH_ENABLED` | false | 是否强制登录（false 时接口开放，账号页仍可用） |
| `SM_AUTH_SECRET_KEY` | 空 | JWT 签名密钥，留空自动生成到 `<数据目录>/.auth_secret` |
| `SM_AUTH_ALLOW_REGISTER` | true | 是否允许自助注册（首个账号自动成为管理员） |
| `SM_ADMIN_USERNAME` / `SM_ADMIN_PASSWORD` | 空 | 启动时确保存在的管理员（幂等，不覆盖已有密码） |
| `SM_WECHAT_LOGIN_MODE` | open | `open` 网站应用扫码 / `mp` 公众号网页授权 / `mock` 本地模拟 / `off` 关闭 |
| `SM_WECHAT_APP_ID` / `SM_WECHAT_APP_SECRET` | 空 | 微信开放平台（或公众号）凭据 |
| `SM_WECHAT_REDIRECT_URI` | 空 | 授权回调，须与微信平台登记完全一致 |
| `SM_DB_AUTO_MIGRATE` | true | 启动时自动迁移；设 false 则要求先 `db upgrade`（版本落后会告警） |
| `SM_BACKUP_KEEP` | 7 | 本地备份保留份数，0 表示不清理 |
| `SM_BACKUP_INCLUDE_SECRET` | true | 备份是否包含 `.auth_secret`（含则归档等同凭据） |

### 账号与微信登录

三种用法，按需选择：

| 场景 | 配置 | 行为 |
|---|---|---|
| 单机自用（默认） | `SM_AUTH_ENABLED=false` | 接口全开放，页面右上角保留「账号入口」，可先建账号再开闸 |
| 多人使用 | `SM_AUTH_ENABLED=true` | 除 `/api/v1/health` 与登录接口外一律要求登录；首个注册账号即管理员 |
| 无凭据想先看流程 | `SM_WECHAT_LOGIN_MODE=mock` | 微信按钮走本地模拟授权页，账号创建/绑定/票据交换全部真跑 |

微信登录两种接入方式：

- **网站应用扫码（推荐，PC 端）**：open.weixin.qq.com 创建「网站应用」→ 拿到 AppID/AppSecret
  → 授权回调域填 `stock.gookeryoung.cn` → `.env` 设
  `SM_WECHAT_LOGIN_MODE=open`，`SM_WECHAT_REDIRECT_URI=https://stock.gookeryoung.cn/api/v1/auth/wechat/callback`。
- **公众号网页授权（微信内打开）**：需已认证服务号，`SM_WECHAT_LOGIN_MODE=mp`，回调域名同样要在
  公众号后台配置为网页授权域名。

首次开启建议用 CLI 建管理员（也可由 `SM_ADMIN_USERNAME/SM_ADMIN_PASSWORD` 自动创建）：

```bash
uv run stockmaster user add --username boss --role admin   # 打印随机初始密码
uv run stockmaster user list
uv run stockmaster user passwd --username boss
uv run stockmaster user role --username boss --status disabled
```

实现要点：密码用 PBKDF2-HMAC-SHA256（60 万轮，参数写在哈希内可平滑升级）；令牌为自签 HS256 JWT，
`token_version` 随改密/禁用/退出登录递增，因此旧令牌立即失效；微信回调只把**一次性 ticket**
带给前端，真正的令牌通过 `POST /auth/ticket` 换取，避免令牌出现在浏览器历史与 Referer 里。

### 数据库迁移与备份恢复

schema 由 ORM 模型定义，迁移链（Alembic）只记录增量；启动时会自动把库带到最新版本，
也可以显式执行：

```bash
uv run stockmaster db current     # 当前版本 / 是否最新（不一致时以非零码退出）
uv run stockmaster db history     # 迁移链与已应用情况
uv run stockmaster db upgrade     # 建表并升级到 head
uv run stockmaster db stamp       # 只写版本标记（认领 create_all 时代的老库）
uv run stockmaster db check       # 比对模型与库结构，报告缺表/缺列
```

备份是**应用级归档**（`.tar.gz` + `manifest.json`），含数据库热备份、模型产物与签名密钥，
可在网页「设置 → 系统备份与恢复」里一键创建/下载/恢复，也可用 CLI：

```bash
uv run stockmaster backup --note "升级前"      # 落到 <数据目录>/backups/
uv run stockmaster backup --list               # 列出本地备份
uv run stockmaster restore <归档> --dry-run    # 只体检，不写入
uv run stockmaster restore <归档> --force      # 覆盖恢复（自动先打一份预备份）
```

与 `deploy/deploy.sh` 的 `backups/pre-deploy-*.tar.gz` 分工不同：部署脚本备份整个
`/data` 卷用于**回滚整个卷**；应用归档可跨机器搬运，恢复时会校验版本、自动迁移 schema、
重置连接池并暂停/恢复定时任务。

### 定时任务（Asia/Shanghai，仅交易日）
`15:20` 日历 → `15:35` 日线 → `16:05` 指数 → `16:30` 指标因子 → `16:45` 预测 → `17:00` 预警 → 盘中每 60s 快照 → 周六 `02:00` 重训。

---

## 8. 已知限制

- **数据不按用户隔离**：自选股、预警规则是全局共享的（账号体系只做访问控制与操作留痕），
  多租户隔离未实现。
- **无邮件/短信找回**：忘记密码需管理员在「用户」页或 CLI 重置；微信登录可作为免密入口。
- 默认仅监听 `127.0.0.1`；对外暴露时**务必**开启 `SM_AUTH_ENABLED=true` 或加一层前置鉴权。
- 仅日线级；不含分钟级、Level-2、实盘下单、融资融券/做空。
- 后复权（hfq）为**按需派生**，不落库（API 会明确返回 501 而非静默降级）。
- 全市场指标/因子历史未落库（只存最新一行）；回测时按需从日线即时重算，以换取更小的库体积。
- 数据源为公开免费接口，存在限流与偶发不可用；系统会降级但**不保证每次成功**。

---

## 9. 许可与数据合规

数据来源于公开接口，请遵守各数据提供方的服务条款，仅用于个人研究。
本项目不内置任何付费数据 Token。
