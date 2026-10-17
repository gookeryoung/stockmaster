"""选股求值器：把「选股配置」翻译成候选清单。

为什么在应用层求值
------------------
条件字段是 ``factors`` 表的横截面列，逐字段比较在 Python 里表达更直接；配置本身
以 JSON 存放（字段会随筛选能力演进增删），SQL 只负责取当日因子与标的元数据。
行为与既有 ``POST /analysis/screen`` 保持一致，避免同一个概念出现两套语义。

空值语义（刻意的两套）
----------------------
* 门槛类（``min_score``/``max_vol``/``min_amount_log``）遇到 ``None`` 视为「不参与
  判定」，与 ``/analysis/screen`` 相同——缺波动率数据的标的不会因为缺数据被误杀；
* 显式条件（``conditions``）遇到 ``None`` 视为**不满足**——「要求 mom_20 >= 0.5」时
  没有动量数据的标的不能算通过，否则条件形同虚设。

排序时 ``None`` 永远排在最后，方向只影响非空值。
"""

from __future__ import annotations

import math
import operator

from sqlalchemy import select
from sqlalchemy.orm import Session

from stockmaster.db.models import Factor, Instrument, ScreeningConfig
from stockmaster.db.scoped import belongs_to
from stockmaster.schemas import ScreeningCondition, ScreeningParams, ScreeningRunResult, ScreenRow
from stockmaster.workspace.service import WorkspaceError

#: 可作为排序/条件字段的因子列（与 ``Factor`` 模型一一对应）。白名单而不是
#: ``getattr`` 任意字段：否则 ``conditions`` 可以读到模型上的非因子属性。
CONDITION_FIELDS: tuple[str, ...] = (
    "score",
    "mom_5",
    "mom_20",
    "mom_60",
    "mom_120",
    "dist_high_252",
    "vol_20",
    "vol_60",
    "atr_pct",
    "turnover_20",
    "amount_log_20",
    "amihud_20",
    "ma_bull",
    "breakout_20",
    "rsi_14",
    "z_mom_20",
    "z_mom_60",
    "z_vol_20",
    "z_turnover_20",
    "z_rsi_14",
    "pct_mom_20",
    "pct_mom_60",
)

ORDER_FIELDS: tuple[str, ...] = CONDITION_FIELDS

_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}


def invalid(detail: str, code: str = "invalid_config") -> WorkspaceError:
    """422：配置内容不合法（不是权限问题，也不是资源不存在）。"""
    return WorkspaceError(detail, status_code=422, code=code)


def validate_params(params: ScreeningParams) -> None:
    """在写入前校验，而不是等求值时报错——坏配置不该先存进库里。"""
    if params.order_by not in ORDER_FIELDS:
        raise invalid(f"不支持按 {params.order_by!r} 排序；可选：{', '.join(ORDER_FIELDS)}")
    for condition in params.conditions:
        validate_condition(condition)


def validate_condition(condition: ScreeningCondition) -> None:
    if condition.field not in CONDITION_FIELDS:
        raise invalid(f"不支持的筛选字段 {condition.field!r}；可选：{', '.join(CONDITION_FIELDS)}")
    if not math.isfinite(condition.value):
        raise invalid(f"筛选条件 {condition.field} 的值必须是有限数字")


def fetch_screening(session: Session, screening_id: int, workspace_id: int) -> ScreeningConfig | None:
    """按 id 取选股配置；不存在或不属于该工作区时返回 ``None``。

    服务层自己判归属（而不是用 ``get_in_workspace_or_404``）是为了让调用方决定
    404 还是 422：直接访问是 404，引用一个已删除的选股配置是 422。
    """
    row = session.get(ScreeningConfig, screening_id)
    if row is None or not belongs_to(row, workspace_id):
        return None
    return row


def _conditions_hold(row: Factor, conditions: list[ScreeningCondition]) -> bool:
    for condition in conditions:
        value = getattr(row, condition.field, None)
        if value is None:
            # 缺数据不等于满足条件：条件筛选是「必须成立」，不是「不能证明不成立」。
            return False
        if not _OPS[condition.op](float(value), float(condition.value)):
            return False
    return True


def _sort_value(row: Factor, field: str) -> float | None:
    value = getattr(row, field, None)
    return None if value is None else float(value)


def latest_factor_date(session: Session):
    return session.execute(select(Factor.trade_date).order_by(Factor.trade_date.desc()).limit(1)).scalar()


def run_screening(session: Session, params: ScreeningParams) -> ScreeningRunResult:
    """按配置对最新一个因子日求值，返回排序、截断后的候选清单。"""
    validate_params(params)

    latest = latest_factor_date(session)
    if latest is None:
        # 没跑过 analytics：返回空结果而不是 404 —— 前端据此提示「先计算因子」。
        return ScreeningRunResult(trade_date=None, count=0, items=[])

    rows = list(session.execute(select(Factor).where(Factor.trade_date == latest)).scalars())
    if not rows:
        return ScreeningRunResult(trade_date=latest, count=0, items=[])

    codes = [row.code for row in rows]
    instruments = {
        item.code: item for item in session.execute(select(Instrument).where(Instrument.code.in_(codes))).scalars()
    }

    matched: list[tuple[Factor, Instrument]] = []
    for row in rows:
        instrument = instruments.get(row.code)
        if instrument is None:
            continue
        if params.exclude_st and instrument.is_st:
            continue
        if params.boards and instrument.board not in params.boards:
            continue
        if params.min_score is not None and (row.score is None or row.score < params.min_score):
            continue
        if params.max_vol is not None and row.vol_20 is not None and row.vol_20 > params.max_vol:
            continue
        if (
            params.min_amount_log is not None
            and row.amount_log_20 is not None
            and row.amount_log_20 < params.min_amount_log
        ):
            continue
        if not _conditions_hold(row, params.conditions):
            continue
        matched.append((row, instrument))

    # None 排在最后：升序用 (is_none, value)，降序用 (is_none, -value)。
    if params.direction == "desc":
        matched.sort(
            key=lambda pair: (
                _sort_value(pair[0], params.order_by) is None,
                -(_sort_value(pair[0], params.order_by) or 0.0),
            )
        )
    else:
        matched.sort(
            key=lambda pair: (
                _sort_value(pair[0], params.order_by) is None,
                _sort_value(pair[0], params.order_by) or 0.0,
            )
        )

    items = [
        ScreenRow(
            code=row.code,
            name=instrument.name,
            trade_date=row.trade_date,
            score=row.score,
            mom_20=row.mom_20,
            mom_60=row.mom_60,
            vol_20=row.vol_20,
            turnover_20=row.turnover_20,
            rsi_14=row.rsi_14,
            close=None,
        )
        for row, instrument in matched[: params.limit]
    ]
    return ScreeningRunResult(trade_date=latest, count=len(items), items=items)


__all__ = [
    "CONDITION_FIELDS",
    "ORDER_FIELDS",
    "fetch_screening",
    "invalid",
    "latest_factor_date",
    "run_screening",
    "validate_condition",
    "validate_params",
]
