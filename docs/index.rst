StockMaster
===========

A 股日线量化分析工作台：数据获取 · 指标与因子 · 预测 · 预警 · 回测。

本页只覆盖文档站入口；项目全貌、方法论与实测结论见仓库根目录的 ``README.md``。

.. toctree::
   :maxdepth: 2
   :caption: 目录

   api
   changelog
   UI_DESIGN
   UI_AUTH
   UI_WORKSPACE
   UI_WATCHLIST

安装
====

后端（Python 3.13，依赖由 uv_ 管理）：

.. code-block:: bash

   uv sync --extra dev

前端（Node 22 + pnpm）：

.. code-block:: bash

   cd frontend && pnpm install

.. _uv: https://docs.astral.sh/uv/

快速上手
========

启动开发环境（后端 API + 前端 Vite，Ctrl+C 一并停止）：

.. code-block:: bash

   make dev

初始化数据库并回补数据：

.. code-block:: bash

   make init
   uv run stockmaster sync-daily --full

常用命令
========

.. code-block:: bash

   make help            # 查看全部 Make 目标

   make test            # 离线测试（网络契约测试默认跳过）
   make cov             # 测试 + 覆盖率门槛
   make test-network    # 网络契约测试（真实数据源，需联网）
   make acceptance      # 端到端实盘验收

   make lint            # ruff check + format 校验
   make check-fast      # 轻量静态门禁（不含测试）
   make check           # 全套门禁

开发约定
========

- 后端代码位于 ``src/stockmaster``，测试位于 ``tests``。
- 质量门禁由 ``make check`` 统一入口，提交前必须本地通过。
- 类型检查目前由 ``make doctor`` 承担（环境自检），项目未引入独立类型检查器。
