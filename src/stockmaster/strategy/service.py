"""策略配置服务：三张工作区级配置表的 CRUD 与校验。

为什么把校验放在服务层
--------------------
与 ``app.workspace.service`` 同一理由：CLI、API、脚本都会写这些表，规则写在路由里
就迟早会被另一条通道绕过。路由只负责把依赖（当前工作区/角色）解析好，规则本身在
这里；``WorkspaceError`` 带 HTTP 语义，由 ``main.py`` 统一翻译。

三张表共享的三条规则
--------------------
1. **名称在「工作区内」唯一**（数据库唯一约束兜底；这里先查一次是为了返回可读的
   409，而不是把 IntegrityError 抛给用户）；
2. **归属检查用 ``belongs_to``**：隔离关闭时退化为恒真，与既有私有表一致；隔离
   开启时跨工作区访问统一 404（不泄露 id 是否存在）；
3. **引用必须同工作区**：算法配置只能绑定本工作区的样本，样本只能引用本工作区的
   选股配置——否则协作边界会被一次「引用」悄悄穿透。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stockmaster.db.models import AlgorithmConfig, SampleConfig, ScreeningConfig
from stockmaster.db.scoped import belongs_to, in_workspace
from stockmaster.schemas import (
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
    ScreeningParams,
    ScreeningRunResult,
)
from stockmaster.strategy.algorithm import train_algorithm, validate_algo_params
from stockmaster.strategy.sample import fetch_sample, parse_codes, resolve_sample
from stockmaster.strategy.screening import fetch_screening, run_screening, validate_params
from stockmaster.workspace.service import WorkspaceError

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 256

UNIVERSE_TYPES = ("watchlist", "screening", "codes", "default")


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #


def invalid(detail: str, code: str = "invalid_config") -> WorkspaceError:
    """422：配置内容不合法。"""
    return WorkspaceError(detail, status_code=422, code=code)


def not_found(kind: str, ident: object) -> WorkspaceError:
    """404：不存在或不属于当前工作区（两种情况刻意不可区分）。"""
    return WorkspaceError(f"{kind} {ident} 不存在", status_code=404, code="config_not_found")


def duplicate(name: str) -> WorkspaceError:
    """409：同一工作区内名称冲突。"""
    return WorkspaceError(f"同名配置已存在：{name}", status_code=409, code="name_taken")


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #


def _params(row: ScreeningConfig) -> ScreeningParams:
    return ScreeningParams.model_validate_json(row.params_json or "{}")


def screening_out(row: ScreeningConfig) -> ScreeningConfigOut:
    return ScreeningConfigOut(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        description=row.description,
        params=_params(row),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def sample_out(row: SampleConfig) -> SampleConfigOut:
    return SampleConfigOut(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        description=row.description,
        universe_type=row.universe_type,  # type: ignore[arg-type]
        universe_value=row.universe_value,
        start_date=row.start_date,
        end_date=row.end_date,
        lookback_days=row.lookback_days,
        horizon=row.horizon,
        min_bars=row.min_bars,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def algorithm_out(row: AlgorithmConfig) -> AlgorithmConfigOut:
    return AlgorithmConfigOut(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        description=row.description,
        algo=row.algo,  # type: ignore[arg-type]
        params=json.loads(row.params_json or "{}"),
        sample_id=row.sample_id,
        horizon=row.horizon,
        test_fraction=row.test_fraction,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --------------------------------------------------------------------------- #
# 公共校验
# --------------------------------------------------------------------------- #


def _clean_name(name: str | None) -> str:
    value = (name or "").strip()
    if not value:
        raise invalid("配置名称不能为空", "invalid_name")
    if len(value) > MAX_NAME_LENGTH:
        raise invalid(f"配置名称最多 {MAX_NAME_LENGTH} 个字符", "invalid_name")
    return value


def _clean_description(description: str | None) -> str:
    return (description or "").strip()[:MAX_DESCRIPTION_LENGTH]


def _ensure_unique(
    session: Session,
    model: type,
    workspace_id: int,
    name: str,
    *,
    exclude_id: int | None = None,
) -> None:
    stmt = select(model.id).where(model.workspace_id == workspace_id, model.name == name)
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    if session.execute(stmt.limit(1)).first() is not None:
        raise duplicate(name)


def _commit(session: Session, name: str) -> None:
    """提交并把唯一约束冲突翻译成 409（并发下先查后插仍可能撞约束）。"""
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise duplicate(name) from exc


def _owned(session: Session, model: type, ident: int, workspace_id: int, kind: str):
    row = session.get(model, ident)
    if row is None or not belongs_to(row, workspace_id):
        raise not_found(kind, ident)
    return row


# --------------------------------------------------------------------------- #
# 选股配置 CRUD
# --------------------------------------------------------------------------- #


def list_screenings(session: Session, workspace_id: int) -> list[ScreeningConfigOut]:
    stmt = in_workspace(select(ScreeningConfig), ScreeningConfig, workspace_id).order_by(
        ScreeningConfig.name, ScreeningConfig.id
    )
    return [screening_out(row) for row in session.execute(stmt).scalars()]


def get_screening(session: Session, config_id: int, workspace_id: int) -> ScreeningConfig:
    return _owned(session, ScreeningConfig, config_id, workspace_id, "选股配置")


def create_screening(
    session: Session,
    workspace_id: int,
    *,
    user_id: int | None,
    payload: ScreeningConfigIn,
) -> ScreeningConfigOut:
    name = _clean_name(payload.name)
    _ensure_unique(session, ScreeningConfig, workspace_id, name)
    validate_params(payload.params)
    row = ScreeningConfig(
        workspace_id=workspace_id,
        name=name,
        description=_clean_description(payload.description),
        params_json=payload.params.model_dump_json(),
        created_by=user_id,
    )
    session.add(row)
    _commit(session, name)
    session.refresh(row)
    return screening_out(row)


def update_screening(
    session: Session,
    config_id: int,
    workspace_id: int,
    payload: ScreeningConfigPatch,
) -> ScreeningConfigOut:
    row = get_screening(session, config_id, workspace_id)
    if payload.name is not None:
        name = _clean_name(payload.name)
        _ensure_unique(session, ScreeningConfig, workspace_id, name, exclude_id=row.id)
        row.name = name
    if payload.description is not None:
        row.description = _clean_description(payload.description)
    if payload.params is not None:
        validate_params(payload.params)
        row.params_json = payload.params.model_dump_json()
    _commit(session, row.name)
    session.refresh(row)
    return screening_out(row)


def delete_screening(session: Session, config_id: int, workspace_id: int) -> None:
    row = get_screening(session, config_id, workspace_id)
    session.delete(row)
    session.commit()


def run_screening_config(session: Session, config_id: int, workspace_id: int) -> ScreeningRunResult:
    row = get_screening(session, config_id, workspace_id)
    return run_screening(session, _params(row))


# --------------------------------------------------------------------------- #
# 样本配置 CRUD
# --------------------------------------------------------------------------- #


def list_samples(session: Session, workspace_id: int) -> list[SampleConfigOut]:
    stmt = in_workspace(select(SampleConfig), SampleConfig, workspace_id).order_by(SampleConfig.name, SampleConfig.id)
    return [sample_out(row) for row in session.execute(stmt).scalars()]


def get_sample(session: Session, config_id: int, workspace_id: int) -> SampleConfig:
    return _owned(session, SampleConfig, config_id, workspace_id, "样本配置")


def _validate_universe(
    session: Session,
    workspace_id: int,
    *,
    universe_type: str,
    universe_value: str,
) -> str:
    """校验标的来源并返回规范化后的 ``universe_value``。"""
    if universe_type not in UNIVERSE_TYPES:
        raise invalid(f"未知的样本来源 {universe_type!r}；可选：{', '.join(UNIVERSE_TYPES)}", "invalid_universe")
    value = (universe_value or "").strip()

    if universe_type == "codes":
        codes = parse_codes(value)
        if not codes:
            raise invalid("codes 来源需要至少一个证券代码", "invalid_universe")
        return ",".join(codes)

    if universe_type == "screening":
        try:
            screening_id = int(value)
        except ValueError as exc:
            raise invalid("screening 来源需要选股配置 id", "invalid_universe") from exc
        if fetch_screening(session, screening_id, workspace_id) is None:
            raise invalid("引用的选股配置不存在（可能已被删除），请重新选择", "screening_not_found")
        return str(screening_id)

    if universe_type == "watchlist":
        return value or "default"

    # default：不使用 universe_value。
    return ""


def _validate_window(
    *,
    start_date,
    end_date,
    lookback_days: int | None,
) -> None:
    if lookback_days is not None and (start_date is not None or end_date is not None):
        raise invalid("lookback_days 不能与 start_date/end_date 同时设置", "invalid_window")
    if start_date is not None and end_date is not None and end_date < start_date:
        raise invalid("end_date 不能早于 start_date", "invalid_window")


def create_sample(
    session: Session,
    workspace_id: int,
    *,
    user_id: int | None,
    payload: SampleConfigIn,
) -> SampleConfigOut:
    name = _clean_name(payload.name)
    _ensure_unique(session, SampleConfig, workspace_id, name)
    value = _validate_universe(
        session,
        workspace_id,
        universe_type=payload.universe_type,
        universe_value=payload.universe_value,
    )
    _validate_window(start_date=payload.start_date, end_date=payload.end_date, lookback_days=payload.lookback_days)
    row = SampleConfig(
        workspace_id=workspace_id,
        name=name,
        description=_clean_description(payload.description),
        universe_type=payload.universe_type,
        universe_value=value,
        start_date=payload.start_date,
        end_date=payload.end_date,
        lookback_days=payload.lookback_days,
        horizon=payload.horizon,
        min_bars=payload.min_bars,
        created_by=user_id,
    )
    session.add(row)
    _commit(session, name)
    session.refresh(row)
    return sample_out(row)


def update_sample(
    session: Session,
    config_id: int,
    workspace_id: int,
    payload: SampleConfigPatch,
) -> SampleConfigOut:
    row = get_sample(session, config_id, workspace_id)
    if payload.name is not None:
        name = _clean_name(payload.name)
        _ensure_unique(session, SampleConfig, workspace_id, name, exclude_id=row.id)
        row.name = name
    if payload.description is not None:
        row.description = _clean_description(payload.description)

    # 三个字段互相约束（lookback 与显式日期互斥、来源值要合法），所以先合并再整体
    # 校验：只校验被 PATCH 的字段会让「新日期 + 旧 lookback」这种组合漏过去。
    merged_type = payload.universe_type if payload.universe_type is not None else row.universe_type
    merged_value = payload.universe_value if payload.universe_value is not None else row.universe_value
    merged_start = payload.start_date if "start_date" in payload.model_fields_set else row.start_date
    merged_end = payload.end_date if "end_date" in payload.model_fields_set else row.end_date
    merged_lookback = payload.lookback_days if "lookback_days" in payload.model_fields_set else row.lookback_days
    if (
        payload.universe_type is not None
        or payload.universe_value is not None
        or "start_date" in payload.model_fields_set
        or "end_date" in payload.model_fields_set
        or "lookback_days" in payload.model_fields_set
    ):
        row.universe_value = _validate_universe(
            session,
            workspace_id,
            universe_type=merged_type,
            universe_value=merged_value,
        )
        _validate_window(start_date=merged_start, end_date=merged_end, lookback_days=merged_lookback)
        row.universe_type = merged_type
        row.start_date = merged_start
        row.end_date = merged_end
        row.lookback_days = merged_lookback

    if payload.horizon is not None:
        row.horizon = payload.horizon
    if payload.min_bars is not None:
        row.min_bars = payload.min_bars
    _commit(session, row.name)
    session.refresh(row)
    return sample_out(row)


def delete_sample(session: Session, config_id: int, workspace_id: int) -> None:
    row = get_sample(session, config_id, workspace_id)
    # 绑定它的算法配置由数据库 ON DELETE SET NULL 解绑，避免级联删掉算法定义。
    session.delete(row)
    session.commit()


def resolve_sample_config(session: Session, config_id: int, workspace_id: int) -> SampleResolveResult:
    row = get_sample(session, config_id, workspace_id)
    return resolve_sample(session, row)


# --------------------------------------------------------------------------- #
# 算法配置 CRUD
# --------------------------------------------------------------------------- #


def list_algorithms(session: Session, workspace_id: int) -> list[AlgorithmConfigOut]:
    stmt = in_workspace(select(AlgorithmConfig), AlgorithmConfig, workspace_id).order_by(
        AlgorithmConfig.name, AlgorithmConfig.id
    )
    return [algorithm_out(row) for row in session.execute(stmt).scalars()]


def get_algorithm(session: Session, config_id: int, workspace_id: int) -> AlgorithmConfig:
    return _owned(session, AlgorithmConfig, config_id, workspace_id, "算法配置")


def _validate_algorithm(
    session: Session,
    workspace_id: int,
    *,
    algo: str,
    params: dict,
    sample_id: int | None,
) -> dict:
    cleaned = validate_algo_params(algo, params)
    if sample_id is not None and fetch_sample(session, sample_id, workspace_id) is None:
        raise invalid("绑定的样本配置不存在（可能已被删除），请重新选择", "sample_not_found")
    return cleaned


def create_algorithm(
    session: Session,
    workspace_id: int,
    *,
    user_id: int | None,
    payload: AlgorithmConfigIn,
) -> AlgorithmConfigOut:
    name = _clean_name(payload.name)
    _ensure_unique(session, AlgorithmConfig, workspace_id, name)
    params = _validate_algorithm(
        session,
        workspace_id,
        algo=payload.algo,
        params=payload.params,
        sample_id=payload.sample_id,
    )
    row = AlgorithmConfig(
        workspace_id=workspace_id,
        name=name,
        description=_clean_description(payload.description),
        algo=payload.algo,
        params_json=json.dumps(params, ensure_ascii=False),
        sample_id=payload.sample_id,
        horizon=payload.horizon,
        test_fraction=payload.test_fraction,
        created_by=user_id,
    )
    session.add(row)
    _commit(session, name)
    session.refresh(row)
    return algorithm_out(row)


def update_algorithm(
    session: Session,
    config_id: int,
    workspace_id: int,
    payload: AlgorithmConfigPatch,
) -> AlgorithmConfigOut:
    row = get_algorithm(session, config_id, workspace_id)
    if payload.name is not None:
        name = _clean_name(payload.name)
        _ensure_unique(session, AlgorithmConfig, workspace_id, name, exclude_id=row.id)
        row.name = name
    if payload.description is not None:
        row.description = _clean_description(payload.description)
    if payload.algo is not None:
        row.algo = payload.algo
    if payload.params is not None or payload.algo is not None or "sample_id" in payload.model_fields_set:
        current_params = json.loads(row.params_json or "{}")
        merged_params = payload.params if payload.params is not None else current_params
        if "sample_id" in payload.model_fields_set:
            merged_sample = payload.sample_id
        else:
            merged_sample = row.sample_id
        cleaned = _validate_algorithm(
            session,
            workspace_id,
            algo=row.algo,
            params=merged_params,
            sample_id=merged_sample,
        )
        row.params_json = json.dumps(cleaned, ensure_ascii=False)
        row.sample_id = merged_sample
    if payload.horizon is not None:
        row.horizon = payload.horizon
    if "test_fraction" in payload.model_fields_set:
        row.test_fraction = payload.test_fraction
    _commit(session, row.name)
    session.refresh(row)
    return algorithm_out(row)


def delete_algorithm(session: Session, config_id: int, workspace_id: int) -> None:
    row = get_algorithm(session, config_id, workspace_id)
    session.delete(row)
    session.commit()


def train_algorithm_config(session: Session, config_id: int, workspace_id: int) -> AlgorithmTrainResult:
    row = get_algorithm(session, config_id, workspace_id)
    return train_algorithm(session, row)


__all__ = [
    "create_algorithm",
    "create_sample",
    "create_screening",
    "delete_algorithm",
    "delete_sample",
    "delete_screening",
    "get_algorithm",
    "get_sample",
    "get_screening",
    "list_algorithms",
    "list_samples",
    "list_screenings",
    "resolve_sample_config",
    "run_screening_config",
    "train_algorithm_config",
    "update_algorithm",
    "update_sample",
    "update_screening",
]
