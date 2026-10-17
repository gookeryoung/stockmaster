"""工作区级策略配置（选股 / 样本 / 算法）。

四个模块各司其职，依赖方向单向：

``screening`` → ``sample`` → ``algorithm`` → ``service``

* ``screening``：把「选股配置」求值成候选清单（只读，viewer 也可运行）；
* ``sample``：把「样本配置」解析成标的池 + 日期窗口 + 预测周期；
* ``algorithm``：用「算法配置」在样本上训练模型（写全局 model_registry）；
* ``service``：三张表的 CRUD、名称/引用校验、工作区归属与重名冲突。

错误统一抛 ``app.workspace.service.WorkspaceError``（带 HTTP 状态码），由
``main.py`` 注册的处理器翻译成 ``{"detail": ...}``，因此服务层不依赖 FastAPI，
CLI 与 API 共用同一套规则。
"""

from app.strategy.algorithm import train_algorithm
from app.strategy.sample import resolve_sample
from app.strategy.screening import run_screening

__all__ = ["resolve_sample", "run_screening", "train_algorithm"]
