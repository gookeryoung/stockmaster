"""工作区（隔离边界）与成员角色。

公开入口是 ``deps``（FastAPI 依赖）与 ``service``（可被 CLI 复用的业务规则），
``roles`` 只提供纯函数判定。
"""

from app.workspace.roles import ROLE_RANK, WorkspaceRole, can_manage, can_write

__all__ = ["ROLE_RANK", "WorkspaceRole", "can_manage", "can_write"]
