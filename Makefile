# Makefile - stockmaster 项目快捷命令
# 运行 `make help` 查看所有可用命令
#
# 约定与 cndb 项目一致：所有 Python 命令经 `uv run` 执行，避免依赖手动激活虚拟环境；
# 中文输出统一通过 `sys.stdout.reconfigure(encoding='utf-8')` 保证在 Windows GBK 控制台不乱码。

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

.PHONY: help sync serve dev init frontend-sync fs frontend-typecheck ft frontend-build fb \
        build b clean c test cov test-network lint format format-check typecheck check-fast check \
        bump patch minor major stats acceptance pack-cache-clean

help: ## 显示帮助信息
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); import re;ms=[(m.group(1),m.group(2).strip()) for f in sys.argv[1:] for l in open(f,encoding='utf-8') if (m:=re.match(r'^([a-zA-Z][\w -]*):.*?##\s*(.*)',l))];[print(f'  {n:<18} {d}') for n,d in ms]" $(MAKEFILE_LIST)

# ── 环境 ─────────────────────────────────────────────────────

sync: ## 安装/更新后端依赖（uv sync --extra dev）
	uv sync --extra dev
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); print('[sync] 后端依赖就绪（含 lint 与 test 工具链）')"

init: sync ## 初始化数据库（建表，幂等）
	uv run stockmaster init

frontend-sync fs: ## 安装前端依赖（pnpm install，已存在 node_modules 则跳过）
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); import os,subprocess;\
print('[frontend] node_modules 已存在，跳过 pnpm install') if os.path.isdir('frontend/node_modules') else (\
print('[frontend] 未安装依赖，开始 pnpm install ...'),\
subprocess.run(['pnpm','install'],cwd='frontend',check=True))"

# ── 运行 ─────────────────────────────────────────────────────

serve: | sync ## 启动服务（后端 API + 已构建前端，单端口）
	uv run stockmaster serve --host $(DEV_HOST) --port $(DEV_PORT)

dev: ## 开发模式说明（Vite 与后端需两个终端，以便各自热更新）
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); print('开发模式需要两个终端：'); print('  终端1: make serve DEV_PORT=$(DEV_PORT)'); print('  终端2: cd frontend && pnpm dev   # http://127.0.0.1:$(FRONTEND_PORT)'); print('Vite 已配置将 /api 代理到 http://127.0.0.1:$(DEV_PORT)')"

stats: | sync ## 显示数据库概况（证券数/日线行数/最新行情日/模型数）
	uv run stockmaster stats

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

typecheck: ## 后端类型检查（当前以 ruff 规则覆盖；预留独立类型检查入口）
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); print('[typecheck] 未配置独立类型检查器（cndb 用 pyrefly，本项目暂由 ruff 规则覆盖）')"

format: ## 后端代码自动格式化（ruff format）
	uv run ruff format backend scripts --cache-dir .ruff_cache
	uv run ruff check backend scripts --cache-dir .ruff_cache --fix

check-fast: lint frontend-typecheck ## 轻量静态门禁（不含测试，适合实现阶段快速反馈）

test: ## 运行测试（不含覆盖率；网络测试默认跳过）
	uv run pytest -n $(PYTEST_JOBS)

cov: ## 运行测试并生成覆盖率报告（HTML + 终端门槛）
	uv run pytest -n $(PYTEST_JOBS) --cov=$(PACKAGE) --cov-report=term-missing --cov-report=html --cov-fail-under=$(COV_THRESHOLD)
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); print('覆盖率报告：htmlcov/index.html')"

test-network: ## 运行网络契约测试（实盘数据源，需联网）
	uv run pytest -m network -v

acceptance: ## 端到端实盘验收（真实数据走完整链路：同步→指标→预测→回测→预警）
	uv run python -u scripts/acceptance_live.py

check: ## 运行全套门禁（lint + 前端类型检查 + 测试 + 覆盖率）
	@$(MAKE) -j3 lint frontend-typecheck cov

# ── 清理与发布 ───────────────────────────────────────────────

clean c: ## 清理构建产物与缓存
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); import shutil,pathlib,glob;\
pts=['build','dist','wheels','*.egg-info','htmlcov','.coverage','.coverage.*','coverage.xml','.ruff_cache','.pytest_cache','.mypy_cache'];\
[shutil.rmtree(m,ignore_errors=True) if pathlib.Path(m).is_dir() else pathlib.Path(m).unlink(missing_ok=True) for p in pts for m in glob.glob(p)];\
[shutil.rmtree(d,ignore_errors=True) for base in ('backend','scripts') for d in pathlib.Path(base).rglob('__pycache__')];\
[f.unlink(missing_ok=True) for base in ('backend','scripts') for f in pathlib.Path(base).rglob('*') if f.suffix in ('.pyc','.pyo')];\
print('[clean] 已清理构建产物与缓存')"

BUMP_PART := $(filter-out bump,$(MAKECMDGOALS))

# uvx 临时环境不受项目 [tool.uv.index] 约束，显式指定镜像源以加速
UVX_INDEX := --default-index https://mirrors.cloud.tencent.com/pypi/simple

bump: ## 版本号 bump（默认 patch，用法：make bump [minor|major]）
	@uvx $(UVX_INDEX) bump-my-version bump $(if $(BUMP_PART),$(firstword $(BUMP_PART)),patch) --tag

patch minor major:
	@:

pack-cache-clean: ## 清理 uv 缓存（uv cache clean）
	uv cache clean
