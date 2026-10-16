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

.. automodule:: app.api
   :members:
   :undoc-members:
