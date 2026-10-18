#!/usr/bin/env python
"""为浏览器端 E2E 测试启动一个「预置数据」的服务实例。

一个进程做三件事，按顺序：

1. 在独立数据目录（默认 ``.e2e/``）里建库并灌入确定性数据 —— 走的是真实的
   SyncService + 真实指标/因子/模型代码，只把 *数据源* 换成 MockProvider，
   所以 e2e 覆盖的仍是生产链路的形状，而不是一堆写死的 JSON。
2. 建好一个管理员账号（密码固定），供 Playwright 登录。
3. 用 uvicorn 起服务，直接托管 ``frontend/dist``（与生产同构：同源、无代理）。

用法：
    python scripts/e2e_server.py --port 8799            # 首次会重建数据
    python scripts/e2e_server.py --port 8799 --reuse    # 复用已有数据目录

刻意不读 ``.env``：E2E 必须完全自洽，任何来自开发者本机的配置都会让结果漂移。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

ADMIN_USERNAME = "e2e-admin"
ADMIN_PASSWORD = "e2e-Passw0rd"
WATCH_CODES = ["600519.SH", "000001.SZ"]


def configure_env(data_dir: Path) -> None:
    """在 import stockmaster.config 之前固定所有环境变量。"""
    os.environ["SM_DATA_DIR"] = str(data_dir)
    os.environ["SM_DB_FILENAME"] = "e2e.db"
    os.environ["SM_SCHEDULER_ENABLED"] = "false"
    # 鉴权必须打开：登录/会话/路由守卫都是 e2e 的关键路径。
    os.environ["SM_AUTH_ENABLED"] = "true"
    os.environ["SM_AUTH_ALLOW_REGISTER"] = "true"
    os.environ["SM_AUTH_FIRST_USER_ADMIN"] = "true"
    os.environ["SM_AUTH_SECRET_KEY"] = "e2e-only-secret-key-not-for-production"
    # 数据源全部走 mock，禁止任何出网请求。
    os.environ["SM_PROVIDER_ORDER"] = "mock"
    os.environ["SM_PROVIDER_OFFLINE"] = "false"
    os.environ["SM_LOG_LEVEL"] = "WARNING"
    # 工作区隔离打开，才能覆盖「切换工作区 = 换数据集」这条路径。
    os.environ["SM_DATA_ISOLATION"] = "true"


def seed() -> None:
    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import init_db, session_scope
    from stockmaster.db import session as session_mod
    from stockmaster.jobs import tasks
    from stockmaster.providers.base import InstrumentInfo
    from stockmaster.providers.mock_provider import MockProvider
    from stockmaster.providers.registry import ProviderRegistry
    from stockmaster.sync import repository as repo
    from stockmaster.sync.sync_service import SyncService

    session_mod.reset_engine()
    init_db()

    service = SyncService(ProviderRegistry([MockProvider()], order=["mock"]))
    end = date.today()
    start = end - timedelta(days=900)

    service.sync_calendar(start, end + timedelta(days=30))
    service.sync_instruments()

    # 仪表盘上的 6 个指数卡片：mock 只声明两只个股，这里补上指数元数据，
    # 否则总览页只能显示空白卡片，e2e 断言会失去意义。
    from stockmaster.core.market import DASHBOARD_INDICES

    with session_scope() as session:
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code=code, name=name, market=code.split(".")[1], board="index", is_index=True, status=1)
                for code, name in DASHBOARD_INDICES
            ],
        )

    service.sync_universe_daily(WATCH_CODES, start=start, end=end, resume=False)
    service.sync_universe_daily([c for c, _ in DASHBOARD_INDICES], start=start, end=end, resume=False)
    service.rebuild_stale_adjustments(WATCH_CODES)

    tasks.job_compute_analytics(WATCH_CODES)
    # 预测页与总览的「上涨概率 Top 10」需要一个已训练模型；样本少，几秒即可。
    tasks.job_train_models(algo="logistic", horizon=1)
    tasks.job_predict(horizon=1)

    from stockmaster.db.models import User  # noqa: F401  (确保表已建)
    from stockmaster.plugins.auth.services import service as auth_service

    with session_scope() as session:
        if auth_service.get_user_by_username(session, ADMIN_USERNAME) is None:
            auth_service.create_user(
                session,
                username=ADMIN_USERNAME,
                password=ADMIN_PASSWORD,
                nickname="E2E 管理员",
                role="admin",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--data-dir", default=str(REPO_ROOT / ".e2e" / "data"))
    parser.add_argument("--reuse", action="store_true", help="复用已有数据目录，不重新灌数据")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    configure_env(data_dir)

    db_file = data_dir / "e2e.db"
    if not args.reuse or not db_file.exists():
        if data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"[e2e] seeding {db_file} ...", flush=True)
        seed()
        print("[e2e] seed done", flush=True)

    dist = REPO_ROOT / "frontend" / "dist" / "index.html"
    if not dist.exists():
        print("[e2e] frontend/dist 不存在，先执行 `pnpm build`（或 make frontend-build）", file=sys.stderr)
        return 2

    import uvicorn

    from stockmaster.main import create_app

    print(f"[e2e] serving http://{args.host}:{args.port}", flush=True)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
