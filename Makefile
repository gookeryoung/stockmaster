# Makefile - stockmaster 项目快捷命令
# 运行 `make help` 查看所有可用命令
#
# 约定：所有 Python 命令经 `uv run` 执行（免手动激活虚拟环境）；
# help/clean/push 等辅助目标内联实现，不引入额外脚本。
# 中文输出统一为 UTF-8（见 README「中文乱码说明」），不做运行时探测。

PACKAGE := stockmaster
# 与 .github/workflows/ci.yml 的 COV_THRESHOLD 保持一致
COV_THRESHOLD := 80

# 服务绑定地址/端口，可在命令行覆盖：make serve DEV_PORT=9000
DEV_HOST ?= 127.0.0.1
DEV_PORT ?= 8770
FRONTEND_PORT ?= 5173

# CPU 档位映射：≤1 核→2 worker，2-3 核→4，其余→8（约 min(cpu*2, 8)）
ifeq ($(OS),Windows_NT)
CPUS := $(NUMBER_OF_PROCESSORS)
else
CPUS := $(shell nproc 2>/dev/null || echo 4)
endif
PYTEST_JOBS := $(if $(filter 0 1,$(CPUS)),2,$(if $(filter 2 3,$(CPUS)),4,8))

.PHONY: help sync init backend serve frontend-dev dev dev-stop stats doctor data-quality \
        frontend-sync fs frontend-typecheck ft frontend-build fb frontend-test ftest frontend-cov fcov e2e \
        build b clean c lint format typecheck check-fast check \
        test test-slow test-all cov test-network acceptance \
        bump patch minor major pub push push-dry

help: ## 显示帮助信息
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); import re;ms=[(m.group(1),m.group(2).strip()) for f in sys.argv[1:] for l in open(f,encoding='utf-8') if (m:=re.match(r'^([a-zA-Z][\w -]*):.*?##\s*(.*)',l))];[print(f'  {n:<16} {d}') for n,d in ms]" $(MAKEFILE_LIST)

# ── 环境 ─────────────────────────────────────────────────────

sync: ## 安装/更新后端依赖（uv sync --extra dev），随后做环境自检
	uv sync --extra dev
	@uv run stockmaster doctor

init: sync ## 初始化数据库（建表，幂等）
	uv run stockmaster init

# ── 运行 ─────────────────────────────────────────────────────

backend: ## 仅启动后端 API（生产模式：托管已构建的前端）
	uv run stockmaster serve --host $(DEV_HOST) --port $(DEV_PORT)

serve: backend ## backend 的别名

frontend-dev: | frontend-sync ## 仅启动前端 Vite 开发服务器（/api 代理到后端）
	cd frontend && pnpm exec vite --host $(DEV_HOST) --port $(FRONTEND_PORT) --strictPort

dev: | frontend-sync ## 开发模式：同一控制台内前台启动前后端，Ctrl+C 一并停止
	uv run stockmaster dev --host $(DEV_HOST) --port $(DEV_PORT) --frontend-port $(FRONTEND_PORT)

dev-stop: ## 清理遗留的开发服务进程（释放 DEV_PORT / FRONTEND_PORT 端口）
	pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -Stop -BackendPort $(DEV_PORT) -FrontendPort $(FRONTEND_PORT)

stats: | sync ## 显示数据库概况（证券数/日线行数/最新行情日/模型数）
	uv run stockmaster stats

doctor: ## 环境自检（Python/依赖/控制台编码/路径/数据源配置）
	uv run stockmaster doctor

data-quality: ## 数据质量体检（覆盖率/缺口/陈旧/异常/复权一致性）
	uv run stockmaster data-quality

# ── 前端 ─────────────────────────────────────────────────────

frontend-sync fs: ## 安装前端依赖（node_modules 已存在则跳过）
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); import os,subprocess;\
print('[frontend] node_modules exists, skip pnpm install') if os.path.isdir('frontend/node_modules') else (\
print('[frontend] installing frontend deps (pnpm install --frozen-lockfile) ...'),\
subprocess.run(['pnpm','install','--frozen-lockfile'],cwd='frontend',check=True))"

frontend-typecheck ft: | frontend-sync ## 前端 TypeScript 类型检查
	cd frontend && pnpm typecheck

frontend-build fb: | frontend-sync ## 构建前端（Vite 产物输出到 frontend/dist，由后端托管）
	cd frontend && pnpm build

frontend-test ftest: | frontend-sync ## 前端单元测试（vitest + Testing Library，不含覆盖率）
	cd frontend && pnpm test

frontend-cov fcov: | frontend-sync ## 前端单测 + 覆盖率（核心模块门槛见 vitest.config.ts）
	cd frontend && pnpm test:coverage

e2e: frontend-build ## 端到端测试（Playwright；webServer 自动起隔离后端，首次先 npx playwright install chromium）
	cd frontend && pnpm e2e

build b: frontend-build ## 构建分发包（前端 → wheel + sdist）
	uv build

# ── 质量门禁 ─────────────────────────────────────────────────

lint: ## 后端代码风格检查（ruff check + format，与 CI 同口径：src + tests + scripts）
	uv run ruff check src tests scripts --cache-dir .ruff_cache
	uv run ruff format --check src tests scripts --cache-dir .ruff_cache

format: ## 后端代码自动格式化（ruff format + check --fix）
	uv run ruff format src tests scripts --cache-dir .ruff_cache
	uv run ruff check src tests scripts --cache-dir .ruff_cache --fix

typecheck: doctor ## doctor 的别名（本项目未引入独立类型检查器）

check-fast cf: lint frontend-typecheck ## 轻量静态门禁（不含测试，适合实现阶段快速反馈）

test: ## 运行快速测试集（排除 slow；网络契约测试默认跳过；不含覆盖率）
	uv run pytest -m "not slow" -n $(PYTEST_JOBS)

test-slow: ## 运行慢测试集（真实子进程/服务器、全链路 e2e；不含覆盖率）
	uv run pytest -m "slow" -n $(PYTEST_JOBS)

test-all: ## 运行全部离线测试（快 + 慢；不含覆盖率）
	uv run pytest -n $(PYTEST_JOBS)

cov: ## 运行快速测试集并生成覆盖率报告（HTML + 终端门槛校验）
	uv run pytest -m "not slow" --cov=$(PACKAGE) --cov-report=term-missing --cov-report=html --cov-fail-under=$(COV_THRESHOLD) -n $(PYTEST_JOBS)
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); print('Coverage report: htmlcov/index.html')"

test-network: ## 运行网络契约测试（真实数据源，需联网）
	uv run pytest -m network -v

acceptance: ## 端到端实盘验收（真实数据走完整链路：同步→指标→预测→回测→预警）
	uv run python -u scripts/acceptance_live.py

check: ## 运行全套门禁（lint + 前端类型检查 + 后端覆盖率 + 前端单测）
	@$(MAKE) -j4 lint frontend-typecheck cov frontend-test

# ── 清理与发布 ───────────────────────────────────────────────

clean c: ## 清理构建产物与缓存（.venv / data / node_modules 始终保留）
	@uv run python -c "import shutil,pathlib,glob;\
pts=['build','dist','wheels','*.egg-info','htmlcov','.coverage','.coverage.*','coverage.xml','.ruff_cache','.mypy_cache','.pytest_cache','reports','.tox','docs/_build','frontend/coverage','frontend/test-results','frontend/playwright-report','frontend/e2e-report'];\
[shutil.rmtree(m,ignore_errors=True) if pathlib.Path(m).is_dir() else pathlib.Path(m).unlink(missing_ok=True) for p in pts for m in glob.glob(p)];\
[shutil.rmtree(d,ignore_errors=True) for base in ('src','tests','scripts') for d in pathlib.Path(base).rglob('__pycache__')];\
[f.unlink(missing_ok=True) for base in ('src','tests','scripts') for f in pathlib.Path(base).rglob('*') if f.suffix in ('.pyc','.pyo')]"

BUMP_PART := $(filter-out bump,$(MAKECMDGOALS))

# uvx 临时环境不受项目 [tool.uv.index] 约束，显式指定镜像源
UVX_INDEX := --default-index https://mirrors.cloud.tencent.com/pypi/simple

bump: ## 版本号 bump（默认 patch，用法：make bump [minor|major]）
	@uvx $(UVX_INDEX) bump-my-version bump $(if $(BUMP_PART),$(firstword $(BUMP_PART)),patch) --tag

patch minor major:
	@:

pub: ## 上传 dist/* 到 PyPI（需先 make build，并配置 PyPI 凭证）
	uvx $(UVX_INDEX) twine upload dist/*.whl dist/*.tar.gz

push: ## 推送当前分支到所有远程仓库（含 tags）
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); import subprocess as sp;\
rs=sp.check_output(['git','remote'],text=True).split();\
[print('[push] '+r,flush=True) or sp.run(['git','push',r,'HEAD','--tags'],check=True) for r in rs];\
print('[push] done: HEAD + tags -> '+', '.join(rs))"

push-dry: ## 演练推送（git push --dry-run，不改远程）
	@uv run python -c "import sys; sys.stdout.reconfigure(encoding='utf-8', errors='replace'); import subprocess as sp;\
rs=sp.check_output(['git','remote'],text=True).split();\
[print('[push] '+r,flush=True) or sp.run(['git','push',r,'HEAD','--tags','--dry-run'],check=True) for r in rs];\
print('[push] dry-run done: '+', '.join(rs))"
