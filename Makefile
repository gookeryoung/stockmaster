# Makefile - stockmaster 项目快捷命令
# 运行 `make help` 查看所有可用命令
#
# 约定与 cndb 项目一致：所有 Python 命令经 `uv run` 执行，避免依赖手动激活虚拟环境。

PACKAGE := app
COV_THRESHOLD := 75

# dev/serve 目标绑定地址与端口，可在命令行覆盖：make serve DEV_PORT=9000
DEV_HOST ?= 127.0.0.1
DEV_PORT ?= 8770
FRONTEND_PORT ?= 5173

# min(cpu*2, 8) 的简化版：≥4 核用 8 个 worker，低核数机器保守降档
ifeq ($(OS),Windows_NT)
CPUS := $(NUMBER_OF_PROCESSORS)
else
CPUS := $(shell nproc 2>/dev/null || echo 4)
endif
PYTEST_JOBS := $(if $(filter 0 1,$(CPUS)),2,$(if $(filter 2 3,$(CPUS)),4,8))

# ── 控制台编码 ───────────────────────────────────────────────
# 中文 Windows 控制台默认代码页为 936(GBK)，而 UTF-8 终端为 65001。
# 若不做处理，`make` 管道中的中文会按错误编码解码而乱码。
# 这里在解析阶段探测一次并导出，供所有子进程（含 make_help.py）使用；
# 探测逻辑放在脚本里而非内联，避免 shell 引号被 sh 改写导致 CreateProcess 失败。
SM_CONSOLE_ENCODING ?= $(shell uv run python scripts/make_help.py --print-encoding 2>/dev/null)
ifneq ($(strip $(SM_CONSOLE_ENCODING)),)
export SM_CONSOLE_ENCODING
endif

.PHONY: help sync serve backend frontend-dev dev dev-stop init frontend-sync fs frontend-typecheck ft \
        frontend-build fb build b clean c test cov test-network lint format format-check \
        typecheck check-fast check bump patch minor major stats doctor acceptance pack-cache-clean

# 帮助文本由 scripts/make_help.py 渲染：该脚本会按终端代码页选择输出编码，
# 因此中文控制台与 UTF-8 终端都能正确显示。
help: ## 显示帮助信息
	@uv run python scripts/make_help.py $(MAKEFILE_LIST)

# ── 环境 ─────────────────────────────────────────────────────

sync: ## 安装/更新后端依赖（uv sync --extra dev）
	uv sync --extra dev
	@uv run stockmaster doctor

init: sync ## 初始化数据库（建表，幂等）
	uv run stockmaster init

# 用 make 的 wildcard 判断目录，避免内联 Python（内联脚本的输出编码不可控，
# 在 GBK 控制台下中文会乱码）
ifeq ($(wildcard frontend/node_modules),)
FRONTEND_NEEDS_INSTALL := 1
endif

frontend-sync fs: ## 安装前端依赖（pnpm install，已存在 node_modules 则跳过）
ifdef FRONTEND_NEEDS_INSTALL
	uv run python scripts/say.py "[frontend] 未安装依赖，开始 pnpm install ..."
	cd frontend && pnpm install
else
	uv run python scripts/say.py "[frontend] node_modules 已存在，跳过 pnpm install"
endif

# ── 运行 ─────────────────────────────────────────────────────

backend: ## 仅启动后端 API（生产模式：托管已构建前端）
	uv run stockmaster serve --host $(DEV_HOST) --port $(DEV_PORT)

serve: backend ## backend 的别名

frontend-dev: | frontend-sync ## 仅启动前端 Vite 开发服务器（代理 /api 到后端）
	cd frontend && pnpm exec vite --host $(DEV_HOST) --port $(FRONTEND_PORT) --strictPort

dev: ## 启动开发环境（后端 + 前端，各自独立窗口，便于分别查看日志与停止）
	pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -BackendPort $(DEV_PORT) -FrontendPort $(FRONTEND_PORT)

dev-stop: ## 停止开发环境（释放 $(DEV_PORT) / $(FRONTEND_PORT) 端口上的进程）
	pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -Stop -BackendPort $(DEV_PORT) -FrontendPort $(FRONTEND_PORT)

stats: | sync ## 显示数据库概况（证券数/日线行数/最新行情日/模型数）
	uv run stockmaster stats

doctor: ## 环境自检（Python/依赖/控制台编码/路径/数据源配置）
	uv run stockmaster doctor

# ── 前端 ─────────────────────────────────────────────────────

frontend-typecheck ft: | frontend-sync ## 前端 TypeScript 类型检查
	cd frontend && pnpm typecheck

frontend-build fb: | frontend-sync ## 构建前端（Vite 产物输出到 frontend/dist，由后端托管）
	cd frontend && pnpm build

build b: frontend-build ## 构建分发包（前端产物 + wheel/sdist）
	uv build

# ── 质量门禁 ─────────────────────────────────────────────────

lint: ## 后端代码风格检查（ruff check + format 校验）
	uv run ruff check backend scripts --cache-dir .ruff_cache
	uv run ruff format --check backend scripts --cache-dir .ruff_cache

typecheck: ## 环境与配置自检（依赖/编码/路径；暂无独立类型检查器）
	uv run stockmaster doctor

format: ## 后端代码自动格式化（ruff format）
	uv run ruff format backend scripts --cache-dir .ruff_cache
	uv run ruff check backend scripts --cache-dir .ruff_cache --fix

check-fast: lint frontend-typecheck ## 轻量静态门禁（不含测试，适合实现阶段快速反馈）

test: ## 运行测试（不含覆盖率；网络测试默认跳过）
	uv run pytest -n $(PYTEST_JOBS)

cov: ## 运行测试并生成覆盖率报告（HTML + 终端门槛）
	uv run pytest -n $(PYTEST_JOBS) --cov=$(PACKAGE) --cov-report=term-missing --cov-report=html --cov-fail-under=$(COV_THRESHOLD)
	uv run python scripts/say.py "覆盖率报告：htmlcov/index.html"

test-network: ## 运行网络契约测试（实盘数据源，需联网）
	uv run pytest -m network -v

acceptance: ## 端到端实盘验收（真实数据走完整链路：同步→指标→预测→回测→预警）
	uv run python -u scripts/acceptance_live.py

check: ## 运行全套门禁（lint + 前端类型检查 + 测试 + 覆盖率）
	@$(MAKE) -j3 lint frontend-typecheck cov

# ── 清理与发布 ───────────────────────────────────────────────

clean c: ## 清理构建产物与缓存（.venv / data / node_modules 始终保留）
	uv run python scripts/clean.py
	uv run python scripts/say.py "如需预演（只列不删）： uv run python scripts/clean.py --dry-run"

BUMP_PART := $(filter-out bump,$(MAKECMDGOALS))

# uvx 临时环境不受项目 [tool.uv.index] 约束，显式指定镜像源以加速
UVX_INDEX := --default-index https://mirrors.cloud.tencent.com/pypi/simple

bump: ## 版本号 bump（默认 patch，用法：make bump [minor|major]）
	@uvx $(UVX_INDEX) bump-my-version bump $(if $(BUMP_PART),$(firstword $(BUMP_PART)),patch) --tag

patch minor major:
	@:

pack-cache-clean: ## 清理 uv 缓存（uv cache clean）
	uv cache clean
