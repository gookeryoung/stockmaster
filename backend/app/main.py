"""FastAPI application assembly.

Serves the JSON API under ``/api/v1`` and, when a built frontend exists, the
React single-page app from the same origin. Single-origin hosting is deliberate:
it means one process and one URL for the user, with no CORS or proxy to configure.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import REPO_ROOT, settings
from app.core.errors import DataError, ProviderUnavailable, StockMasterError
from app.core.logging import get_logger, setup_logging

log = get_logger(__name__)

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


@contextlib.asynccontextmanager
async def lifespan(
    app: FastAPI,  # noqa: ARG001 - required by the FastAPI lifespan protocol
) -> AsyncIterator[None]:
    """Initialise the database and scheduler on startup; clean up on shutdown."""
    setup_logging(settings.log_level)
    settings.ensure_dirs()

    from app.db import init_db

    init_db()

    from app.jobs.scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    log.info("StockMaster %s ready on http://%s:%d", __version__, settings.host, settings.port)
    try:
        yield
    finally:
        stop_scheduler()
        # Release provider sockets explicitly; baostock holds a long-lived one.
        try:
            from app.providers.registry import get_registry

            get_registry().close()
        except Exception as exc:
            log.debug("provider shutdown raised: %s", exc)


def create_app() -> FastAPI:
    """Build the application."""
    app = FastAPI(
        title="StockMaster",
        description=(
            "A 股日线量化分析工作台：数据获取、指标与因子分析、机器学习预测、"
            "回测验证与规则预警。仅供研究参考，不构成投资建议。"
        ),
        version=__version__,
        lifespan=lifespan,
    )

    # The frontend is served from the same origin in production; CORS exists only
    # so the Vite dev server (different port) can talk to the API during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- API routers ---
    from app.api.v1 import alerts, analysis, backtest, market, predict, system

    api_prefix = "/api/v1"
    app.include_router(market.router, prefix=api_prefix)
    app.include_router(analysis.router, prefix=api_prefix)
    app.include_router(predict.router, prefix=api_prefix)
    app.include_router(backtest.router, prefix=api_prefix)
    app.include_router(alerts.router, prefix=api_prefix)
    app.include_router(system.router, prefix=api_prefix)

    # --- error handling: convert domain errors into meaningful HTTP responses ---
    @app.exception_handler(ProviderUnavailable)
    async def _provider_unavailable(_request: Request, exc: ProviderUnavailable):
        return JSONResponse(
            status_code=503,
            content={
                "detail": str(exc),
                "hint": "所有数据源均不可用；请检查网络，或在系统状态页查看熔断状态。",
            },
        )

    @app.exception_handler(DataError)
    async def _data_error(_request: Request, exc: DataError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(StockMasterError)
    async def _domain_error(_request: Request, exc: StockMasterError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # --- static frontend (only when built) ---
    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA, with client-side routing fallback."""
    if not FRONTEND_DIST.exists():
        log.info(
            "frontend build not found at %s; API-only mode (run `pnpm build` in frontend/ to enable the UI)",
            FRONTEND_DIST,
        )

        @app.get("/", include_in_schema=False)
        def api_only_root():
            return {
                "name": "StockMaster",
                "version": __version__,
                "mode": "api-only",
                "docs": "/docs",
                "message": "前端未构建。请在 frontend/ 执行 pnpm install && pnpm build。",
            }

        return

    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index_file = FRONTEND_DIST / "index.html"

    @app.get("/", include_in_schema=False)
    def spa_root():
        return FileResponse(index_file)

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """Serve static files when they exist, otherwise index.html for the SPA."""
        # Never shadow the API or docs.
        if full_path.startswith(("api/", "docs", "openapi.json", "redoc")):
            return JSONResponse(status_code=404, content={"detail": "not found"})

        candidate = (FRONTEND_DIST / full_path).resolve()
        try:
            candidate.relative_to(FRONTEND_DIST.resolve())
        except ValueError:
            # Path traversal attempt: fall back to the SPA shell.
            return FileResponse(index_file)
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_file)


app = create_app()
