API 参考
=========

顶层包
------

.. automodule:: stockmaster
   :members:
   :undoc-members:
   :show-inheritance:

核心领域模块
------------

以下模块按项目实际布局（``src/stockmaster``）列出，与 ``pyproject.toml`` 的
``pythonpath = ["src"]`` 一致。

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

预测与回测
~~~~~~~~~~

.. automodule:: stockmaster.model
   :members:
   :undoc-members:

.. automodule:: stockmaster.backtest
   :members:
   :undoc-members:

预警与接口
~~~~~~~~~~

.. automodule:: stockmaster.alert
   :members:
   :undoc-members:

账号与登录
~~~~~~~~~~

密码哈希、JWT、微信 OAuth 与账号服务。``SM_AUTH_ENABLED=false`` 时网关不拦截，
但账号页与令牌校验仍然可用。

.. automodule:: stockmaster.maintenance

.. automodule:: stockmaster.auth
   :members:
   :undoc-members:

工作区与隔离
~~~~~~~~~~~~

cndb 式工作区（owner/editor/viewer 角色），``SM_DATA_ISOLATION=true`` 时启用；
设计见 ``docs/WORKSPACE.md``。

.. automodule:: stockmaster.workspace
   :members:
   :undoc-members:

.. automodule:: stockmaster.api
   :members:
   :undoc-members:
