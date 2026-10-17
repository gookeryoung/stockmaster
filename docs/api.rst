API 参考
=========

顶层包
------

.. automodule:: app
   :members:
   :undoc-members:
   :show-inheritance:

核心领域模块
------------

以下模块按项目实际布局（``backend/app``）列出，与 ``pyproject.toml`` 的
``pythonpath = ["backend"]`` 一致。

数据源
~~~~~~

.. automodule:: app.providers
   :members:
   :undoc-members:

指标与因子
~~~~~~~~~~

.. automodule:: app.indicators
   :members:
   :undoc-members:

.. automodule:: app.factors
   :members:
   :undoc-members:

预测与回测
~~~~~~~~~~

.. automodule:: app.model
   :members:
   :undoc-members:

.. automodule:: app.backtest
   :members:
   :undoc-members:

预警与接口
~~~~~~~~~~

.. automodule:: app.alert
   :members:
   :undoc-members:

账号与登录
~~~~~~~~~~

密码哈希、JWT、微信 OAuth 与账号服务。``SM_AUTH_ENABLED=false`` 时网关不拦截，
但账号页与令牌校验仍然可用。

.. automodule:: app.auth
   :members:
   :undoc-members:

.. automodule:: app.api
   :members:
   :undoc-members:
