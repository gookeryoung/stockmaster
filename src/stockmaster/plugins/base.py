"""插件基类定义：所有业务域插件的统一契约。

约定
----
每个插件是 ``stockmaster/plugins/<name>/`` 子包，包含：

* ``plugin.py`` —— 一个 ``PluginBase`` 子类（类属性 ``name`` 即插件名）与 ``plugin`` 实例；
* ``routers/``  —— FastAPI 路由；``register_routes`` 把它们 include 进注册中心传入的 router；
* ``services/`` —— 业务服务层（服务层不依赖 FastAPI，CLI 与 jobs 可复用）；
* ``schemas/``  —— 该插件私有或对外暴露的 Pydantic 模型（可为空包）。

路由前缀由注册中心统一施加：``/api/v1`` 加上可选的 ``route_prefix``（多数插件为空串，
因为路径装饰器里已写全领域段；``workspaces`` 用 ``route_prefix`` 实现挂载前缀）。

依赖方向
--------
单向：插件可以 import ``stockmaster.core`` / ``db`` / ``providers`` / ``sync`` 等共享基础设施，
反之共享基础设施不得 import 某个插件的 ``services``。本仓库有两处刻意例外：
``core/auth_deps.py`` 与 ``core/workspace_deps.py`` 引用了 auth / workspaces 插件的服务模块，
因为「账号」与「工作区」这两个领域的所有者就是对应插件，把它们的服务再复制一份到 core
反而会造成双份真相。插件之间也可以单向引用（如 backtest→predict）。

模型注册
--------
本仓库 ORM 模型集中在 ``stockmaster.db.models``，插件无需覆写 ``register_models``；
该钩子保留是为了对齐参考项目 cndb 的插件接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fastapi import APIRouter


class PluginBase(ABC):
    """插件抽象基类，所有业务域插件必须继承此类。

    子类需定义 ``name``（同时是默认路由段来源）与 ``description`` 等类属性，
    并实现 ``register_routes``；``register_models`` 可选覆写。
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    # 自定义路由前缀：空串表示直接挂在 /api/v1 之下；非空时作为 /api/v1 之后的路径段。
    route_prefix: str = ""

    @abstractmethod
    def register_routes(self, router: APIRouter) -> None:
        """把本插件的路由注册到给定 router。

        实现里通常 include 自己 ``routers/`` 下的子模块（惰性导入以避免循环依赖）。
        """

    # B027: ABC 钩子允许空实现（与参考项目 cndb 的约定一致）
    def register_models(self) -> None:  # noqa: B027
        """注册数据模型（本仓库模型集中在 db/models.py，默认空实现）。"""


__all__ = ["PluginBase"]
