"""样本解析：把「样本配置」变成标的池 + 日期窗口 + 预测周期。

``universe_type`` 的四种来源
----------------------------
* ``watchlist`` —— 本工作区某个自选清单（清单名存在 ``universe_value``）；
* ``screening`` —— 引用一个选股配置，求值得到候选池；
* ``codes`` —— 逗号/空白分隔的显式代码，解析时统一规范化；
* ``default`` —— 交给 ``_default_universe()``（自选并集 → 指数成分 → 全部标的）。

日期窗口
--------
显式 ``start_date``/``end_date`` 与 ``lookback_days`` 互斥（在 schema 与服务层双重
校验）：同时给会让「以哪个为准」变成隐式约定，两边算出不同样本却都自称同一个配置。
``lookback_days`` 以库中最新交易日为终点向前推，而不是以「今天」——周末/停牌时
「今天」根本不在数据里。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import DataError
from app.core.symbols import normalize
from app.db.models import DailyBar, SampleConfig, WatchlistItem
from app.db.scoped import belongs_to
from app.schemas import SampleResolveResult, ScreeningParams
from app.strategy.screening import fetch_screening, invalid, run_screening

_SPLIT = re.compile(r"[,;\s，、]+")


def parse_codes(value: str) -> list[str]:
    """把显式代码串解析成规范形式；非法代码直接 422，而不是静默丢弃。"""
    codes: list[str] = []
    seen: set[str] = set()
    for token in _SPLIT.split((value or "").strip()):
        token = token.strip()
        if not token:
            continue
        try:
            canon = normalize(token)
        except DataError as exc:
            raise invalid(f"无效的证券代码 {token!r}：{exc}") from exc
        if canon not in seen:
            seen.add(canon)
            codes.append(canon)
    return codes


def fetch_sample(session: Session, sample_id: int, workspace_id: int) -> SampleConfig | None:
    """按 id 取样本配置；不存在或不属于该工作区时返回 ``None``。"""
    row = session.get(SampleConfig, sample_id)
    if row is None or not belongs_to(row, workspace_id):
        return None
    return row


def _universe_codes(session: Session, sample: SampleConfig) -> list[str]:
    kind = sample.universe_type
    value = (sample.universe_value or "").strip()

    if kind == "codes":
        return parse_codes(value)

    if kind == "watchlist":
        list_name = value or "default"
        rows = session.execute(
            select(WatchlistItem.code)
            .where(
                WatchlistItem.workspace_id == sample.workspace_id,
                WatchlistItem.list_name == list_name,
            )
            .order_by(WatchlistItem.id)
        ).scalars()
        return list(dict.fromkeys(rows))

    if kind == "screening":
        try:
            screening_id = int(value)
        except ValueError as exc:
            raise invalid("引用的选股配置 id 必须是整数") from exc
        screening = fetch_screening(session, screening_id, sample.workspace_id)
        if screening is None:
            raise invalid("引用的选股配置不存在（可能已被删除），请重新选择", "screening_not_found")
        params = ScreeningParams.model_validate_json(screening.params_json or "{}")
        return [item.code for item in run_screening(session, params).items]

    # default：调度器语义的默认宇宙；隔离模式下按工作区取，避免把全站自选当成
    # 某个人的训练样本。
    from app.jobs.tasks import _default_universe

    workspace_id = sample.workspace_id if settings.data_isolation else None
    return _default_universe(workspace_id=workspace_id)


def _resolve_window(session: Session, sample: SampleConfig) -> tuple[date | None, date | None]:
    start = sample.start_date
    end = sample.end_date
    if sample.lookback_days is None:
        return start, end

    latest = session.execute(select(func.max(DailyBar.trade_date))).scalar()
    if latest is None:
        raise invalid("数据库里没有任何日线数据，无法按 lookback_days 计算样本窗口", "no_market_data")
    end = end or latest
    start = end - timedelta(days=sample.lookback_days)
    return start, end


def resolve_sample(session: Session, sample: SampleConfig) -> SampleResolveResult:
    """解析样本配置，返回标的池、日期窗口与预测周期。"""
    codes = _universe_codes(session, sample)
    start, end = _resolve_window(session, sample)
    if start is not None and end is not None and start > end:
        raise invalid("样本窗口的开始日期不能晚于结束日期")
    return SampleResolveResult(
        codes=codes,
        count=len(codes),
        start=start,
        end=end,
        horizon=sample.horizon,
    )


__all__ = ["fetch_sample", "parse_codes", "resolve_sample"]
