"""插件注册中心 —— 自动发现、加载并把插件路由挂到 FastAPI app 上。

插件目录约定：``stockmaster/plugins/<name>/plugin.py`` 里定义 ``PluginBase`` 子类。
``discover_and_load()`` 扫描 plugins 包的每个子包并实例化其中的插件；
``mount_routes(app)`` 把它们统一挂到 ``/api/v1`` 之下。

与参考项目 cndb 的两点刻意偏离
------------------------------
* **不注入 openapi ``tags``**：``include_router`` 只传 ``prefix``，保持 openapi.json 逐字不变；
* **不提供 on_startup/on_shutdown 钩子**：调度器生命周期仍由 ``main`` 的 lifespan 显式
  管理，鉴权中间件仍由 ``main`` 显式 ``add_middleware``，插件保持「纯路由 + 服务」。
"""

from __future__ import annotations

import importlib
import inspect
import weakref
from pathlib import Path

from fastapi import APIRouter, FastAPI

from stockmaster.core.logging import get_logger
from stockmaster.plugins.base import PluginBase

log = get_logger(__name__)

#: 所有插件路由共享的挂载前缀（与重构前的 ``main.create_app`` 一致）。
API_V1_PREFIX = "/api/v1"


class PluginRegistry:
    """插件注册中心，应用生命周期内唯一。

    生命周期：

    1. ``discover_and_load()`` —— 扫描 plugins 包，发现并实例化所有插件；
    2. ``mount_routes(app)``   —— 把每个插件的路由挂到 FastAPI app 上（同一 app 只挂一次）。
    """

    def __init__(self) -> None:
        self._plugins: dict[str, PluginBase] = {}
        self._loaded = False
        # 弱引用集合：避免用 id(app) 判重——app 被回收后新 app 会复用同一个 id，
        # 导致后续 create_app 的挂载被误判为「已挂载」而全部丢失路由。
        self._mounted_apps: weakref.WeakSet[FastAPI] = weakref.WeakSet()

    # ── 注册 ─────────────────────────────────────────────────────────────
    def register(self, plugin: PluginBase) -> None:
        """注册一个插件实例；同名插件重复注册会被跳过（不覆盖）。"""
        if plugin.name in self._plugins:
            log.warning("插件 %s 已存在，跳过重复注册", plugin.name)
            return
        plugin.register_models()
        self._plugins[plugin.name] = plugin
        log.info("插件已注册：%s v%s", plugin.name, plugin.version)

    def discover_and_load(self) -> None:
        """自动发现并加载 plugins 包下的所有插件。

        扫描规则：跳过 ``base.py`` / ``registry.py`` / ``__pycache__`` 等非子包项，
        导入 ``stockmaster.plugins.<name>.plugin``，取其中首个 ``PluginBase`` 非抽象子类并实例化。

        幂等：插件是进程内单例，重复调用（如测试里多次 ``create_app``）不再重复扫描。
        """
        if self._loaded:
            log.debug("插件已加载，跳过重复扫描")
            return
        self._loaded = True

        plugins_dir = Path(__file__).resolve().parent
        for item in sorted(plugins_dir.iterdir()):
            if not item.is_dir() or item.name.startswith(("_", ".")):
                continue
            try:
                module = importlib.import_module(f"stockmaster.plugins.{item.name}.plugin")
            except ImportError as exc:  # pragma: no cover - 子包缺少 plugin.py
                log.debug("跳过 %s：%s", item.name, exc)
                continue
            except Exception as exc:  # pragma: no cover - 插件加载异常
                log.error("加载插件 %s 失败：%s", item.name, exc)
                continue

            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, PluginBase)
                    and attr is not PluginBase
                    and not inspect.isabstract(attr)
                ):
                    self.register(attr())
                    break

    # ── 路由挂载 ─────────────────────────────────────────────────────────
    def mount_routes(self, app: FastAPI) -> None:
        """把每个插件的路由挂到 app 上（幂等：同一 app 实例只挂一次）。"""
        if app in self._mounted_apps:
            log.debug("app 已挂载过插件路由，跳过")
            return

        for name, plugin in self._plugins.items():
            # 插件若直接暴露 ``direct_router`` 就跳过中间 APIRouter 包装（与 cndb 一致）：
            # workspaces 前缀下存在路径为空的端点（``GET /api/v1/workspaces``），
            # 多一层 include_router 中转会触发 FastAPI「前缀与路径不能同时为空」。
            router = getattr(plugin, "direct_router", None)
            if router is None:
                router = APIRouter()
                plugin.register_routes(router)
            prefix = API_V1_PREFIX + (f"/{plugin.route_prefix}" if plugin.route_prefix else "")
            app.include_router(router, prefix=prefix)
            log.info("插件路由已挂载：%s -> %s", name, prefix)

        self._mounted_apps.add(app)


# 全局单例（应用生命周期内唯一）
registry = PluginRegistry()

__all__ = ["API_V1_PREFIX", "PluginRegistry", "registry"]
