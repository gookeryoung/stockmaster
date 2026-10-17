"""策略配置 API：工作区级自定义的选股 / 样本 / 算法。

权限分层（与 ``docs/WORKSPACE.md`` 的角色矩阵一致）
--------------------------------------------------
* 读端点（列表/详情）：``current_workspace`` —— 成员即可，viewer 也能看；
* 只读求值（跑选股、解析样本）：``current_workspace`` —— 不写库，viewer 可以用它
  验证别人定义的策略；
* 写端点（增删改）与算法训练：``require_editor`` —— viewer 403
  + ``X-Auth-Error: read_only``；训练会写全局模型注册表，因此也归为写操作。

跨工作区访问统一 404：服务层用 ``belongs_to`` 判归属，不泄露 id 是否存在。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.schemas import (
    AlgorithmConfigIn,
    AlgorithmConfigOut,
    AlgorithmConfigPatch,
    AlgorithmTrainResult,
    SampleConfigIn,
    SampleConfigOut,
    SampleConfigPatch,
    SampleResolveResult,
    ScreeningConfigIn,
    ScreeningConfigOut,
    ScreeningConfigPatch,
    ScreeningRunResult,
)
from app.strategy import service
from app.workspace.deps import WorkspaceCtx, current_workspace, require_editor

router = APIRouter(tags=["strategy"])


def _user_id(ws: WorkspaceCtx) -> int | None:
    return ws.user.id if ws.user is not None else None


# --------------------------------------------------------------------------- #
# 选股配置
# --------------------------------------------------------------------------- #


@router.get("/strategy/screenings", response_model=list[ScreeningConfigOut])
def list_screenings(
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    return service.list_screenings(session, ws.id)


@router.post("/strategy/screenings", response_model=ScreeningConfigOut, status_code=status.HTTP_201_CREATED)
def create_screening(
    payload: ScreeningConfigIn,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    return service.create_screening(session, ws.id, user_id=_user_id(ws), payload=payload)


@router.get("/strategy/screenings/{config_id}", response_model=ScreeningConfigOut)
def get_screening(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    return service.screening_out(service.get_screening(session, config_id, ws.id))


@router.patch("/strategy/screenings/{config_id}", response_model=ScreeningConfigOut)
def update_screening(
    config_id: int,
    payload: ScreeningConfigPatch,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    return service.update_screening(session, config_id, ws.id, payload)


@router.delete("/strategy/screenings/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_screening(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    service.delete_screening(session, config_id, ws.id)


@router.post("/strategy/screenings/{config_id}/run", response_model=ScreeningRunResult)
def run_screening_config(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    """运行选股配置（只读）：返回最新因子日的候选清单。"""
    return service.run_screening_config(session, config_id, ws.id)


# --------------------------------------------------------------------------- #
# 样本配置
# --------------------------------------------------------------------------- #


@router.get("/strategy/samples", response_model=list[SampleConfigOut])
def list_samples(
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    return service.list_samples(session, ws.id)


@router.post("/strategy/samples", response_model=SampleConfigOut, status_code=status.HTTP_201_CREATED)
def create_sample(
    payload: SampleConfigIn,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    return service.create_sample(session, ws.id, user_id=_user_id(ws), payload=payload)


@router.get("/strategy/samples/{config_id}", response_model=SampleConfigOut)
def get_sample(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    return service.sample_out(service.get_sample(session, config_id, ws.id))


@router.patch("/strategy/samples/{config_id}", response_model=SampleConfigOut)
def update_sample(
    config_id: int,
    payload: SampleConfigPatch,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    return service.update_sample(session, config_id, ws.id, payload)


@router.delete("/strategy/samples/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sample(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    service.delete_sample(session, config_id, ws.id)


@router.post("/strategy/samples/{config_id}/resolve", response_model=SampleResolveResult)
def resolve_sample_config(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    """解析样本配置（只读）：标的池、日期窗口与预测周期。"""
    return service.resolve_sample_config(session, config_id, ws.id)


# --------------------------------------------------------------------------- #
# 算法配置
# --------------------------------------------------------------------------- #


@router.get("/strategy/algorithms", response_model=list[AlgorithmConfigOut])
def list_algorithms(
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    return service.list_algorithms(session, ws.id)


@router.post("/strategy/algorithms", response_model=AlgorithmConfigOut, status_code=status.HTTP_201_CREATED)
def create_algorithm(
    payload: AlgorithmConfigIn,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    return service.create_algorithm(session, ws.id, user_id=_user_id(ws), payload=payload)


@router.get("/strategy/algorithms/{config_id}", response_model=AlgorithmConfigOut)
def get_algorithm(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(current_workspace),
):
    return service.algorithm_out(service.get_algorithm(session, config_id, ws.id))


@router.patch("/strategy/algorithms/{config_id}", response_model=AlgorithmConfigOut)
def update_algorithm(
    config_id: int,
    payload: AlgorithmConfigPatch,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    return service.update_algorithm(session, config_id, ws.id, payload)


@router.delete("/strategy/algorithms/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_algorithm(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    service.delete_algorithm(session, config_id, ws.id)


@router.post("/strategy/algorithms/{config_id}/train", response_model=AlgorithmTrainResult)
def train_algorithm_config(
    config_id: int,
    session: Session = Depends(db_session),
    ws: WorkspaceCtx = Depends(require_editor),
):
    """按算法配置训练并注册模型（写操作：viewer 403）。"""
    return service.train_algorithm_config(session, config_id, ws.id)
