API 参考
=========

顶层包
------

.. automodule:: stockmaster
   :members:
   :undoc-members:
   :show-inheritance:

共享基础设施
------------

以下模块按项目实际布局（``src/stockmaster``）列出，与 ``pyproject.toml`` 的
``pythonpath = ["src"]`` 一致。业务域已插件化到 ``stockmaster.plugins`` 之下。

插件机制
~~~~~~~~

.. automodule:: stockmaster.plugins.base
   :members:
   :undoc-members:

.. automodule:: stockmaster.plugins.registry
   :members:
   :undoc-members:

数据源
~~~~~~

.. automodule:: stockmaster.providers
   :members:
   :undoc-members:

指标与因子
~~~~~~~~~~

.. automodule:: stockmaster.indicators
   :members:
   :undoc-members:

.. automodule:: stockmaster.factors
   :members:
   :undoc-members:

数据库
~~~~~~

.. automodule:: stockmaster.db
   :members:
   :undoc-members:

业务插件
--------

每个插件是 ``stockmaster/plugins/<name>/`` 子包，含 ``plugin.py``、``routers/``
与 ``services/``。

行情与总览
~~~~~~~~~~

.. automodule:: stockmaster.plugins.market
   :members:
   :undoc-members:

.. automodule:: stockmaster.plugins.analysis
   :members:
   :undoc-members:

预测与回测
~~~~~~~~~~

.. automodule:: stockmaster.plugins.predict
   :members:
   :undoc-members:

.. automodule:: stockmaster.plugins.backtest
   :members:
   :undoc-members:

预警与策略
~~~~~~~~~~

.. automodule:: stockmaster.plugins.alerts
   :members:
   :undoc-members:

.. automodule:: stockmaster.plugins.strategy
   :members:
   :undoc-members:

系统与维护
~~~~~~~~~~

.. automodule:: stockmaster.plugins.system
   :members:
   :undoc-members:

账号与登录
~~~~~~~~~~

密码哈希、JWT、微信 OAuth 与账号服务。``SM_AUTH_ENABLED=false`` 时网关不拦截，
但账号页与令牌校验仍然可用。

.. automodule:: stockmaster.plugins.auth
   :members:
   :undoc-members:

工作区与隔离
~~~~~~~~~~~~

cndb 式工作区（owner/editor/viewer 角色），``SM_DATA_ISOLATION=true`` 时启用；
设计见 ``docs/WORKSPACE.md``。

.. automodule:: stockmaster.plugins.workspaces
   :members:
   :undoc-members:
