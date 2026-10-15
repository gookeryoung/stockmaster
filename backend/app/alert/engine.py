"""Alert evaluation engine.

Two properties carry most of the weight here:

* **Idempotent triggering.** Every trigger derives a ``dedup_key`` that encodes
  the rule's semantic window (once per day, or once per cooldown). Combined with
  the unique ``(rule_id, dedup_key)`` constraint, running the scanner repeatedly
  — every minute, or twice after a crash — cannot produce duplicate alerts. This
  is what stops a "price above 100" rule from firing thousands of times.
* **Explainability.** Each trigger carries a human-readable message and the
  numeric snapshot that produced it, so a user can audit why it fired.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.alert.rules import RULE_TYPES, RuleContext, describe_rule, normalize_params
from app.core.errors import DataError
from app.core.logging import get_logger
from app.core.market import is_call_auction, is_trading_time
from app.db.models import AlertRule, AlertTrigger, Instrument, WatchlistItem
from app.db.session import session_scope

log = get_logger(__name__)

__all__ = ["TriggerCandidate", "evaluate_rule", "AlertEngine"]


@dataclass
class TriggerCandidate:
    """A rule that fired for one symbol, before deduplication."""

    rule_id: int
    code: str
    name: str
    trade_date: date
    rule_type: str
    message: str
    snapshot: dict
    dedup_key: str
    trigger_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


def _pct_change(ctx: RuleContext) -> float | None:
    if ctx.pct_chg is not None:
        return ctx.pct_chg
    if ctx.close is not None and ctx.preclose:
        return (ctx.close / ctx.preclose - 1.0) * 100.0
    return None


def evaluate_rule(ctx: RuleContext, rule_type: str, params: dict) -> tuple[bool, str, dict]:
    """Evaluate one rule against one context.

    Returns:
        ``(triggered, message, snapshot)``. ``snapshot`` always carries the
        observed values so a trigger is auditable even for a simple threshold.
    """
    params = normalize_params(rule_type, params)

    if rule_type == "price_cross":
        close = ctx.close
        if close is None:
            return False, "", {}
        threshold = params["threshold"]
        fired = close >= threshold if params["direction"] == "above" else close <= threshold
        message = (
            f"{ctx.name or ctx.code} 收盘 {close:.2f} "
            f"{'上穿' if params['direction'] == 'above' else '下穿'} {threshold:.2f}"
        )
        return fired, message if fired else "", {"close": close, "threshold": threshold}

    if rule_type == "pct_change":
        change = _pct_change(ctx)
        if change is None:
            return False, "", {}
        threshold = params["threshold"]
        fired = change >= threshold if params["direction"] == "above" else change <= threshold
        message = f"{ctx.name or ctx.code} 涨跌幅 {change:+.2f}% 触发阈值 {threshold:.2f}%"
        return fired, message if fired else "", {"pct_chg": change, "threshold": threshold}

    if rule_type == "price_change_pct":
        reference = ctx.close if params["reference"] == "close" else ctx.preclose
        if ctx.close is None or not reference:
            return False, "", {}
        change = (ctx.close / reference - 1.0) * 100.0
        threshold = params["threshold"]
        fired = change >= threshold if params["direction"] == "above" else change <= threshold
        message = (
            f"{ctx.name or ctx.code} 相对{params['reference']}变动 {change:+.2f}% "
            f"触发阈值 {threshold:.2f}%"
        )
        return fired, message if fired else "", {"change_pct": change, "threshold": threshold}

    if rule_type == "volume_ratio":
        ratio = ctx.indicators.get("vol_ratio")
        if ratio is None or ratio != ratio:
            return False, "", {}
        threshold = params["threshold"]
        fired = ratio >= threshold if params["direction"] == "above" else ratio <= threshold
        message = f"{ctx.name or ctx.code} 量比 {ratio:.2f} 触发阈值 {threshold:.2f}"
        return fired, message if fired else "", {"vol_ratio": ratio, "threshold": threshold}

    if rule_type == "turnover":
        turn = ctx.turn
        if turn is None:
            return False, "", {}
        threshold = params["threshold"]
        fired = turn >= threshold if params["direction"] == "above" else turn <= threshold
        message = f"{ctx.name or ctx.code} 换手率 {turn:.2f}% 触发阈值 {threshold:.2f}%"
        return fired, message if fired else "", {"turn": turn, "threshold": threshold}

    if rule_type == "ma_cross":
        fast = ctx.indicators.get(f"ma{params['fast']}")
        slow = ctx.indicators.get(f"ma{params['slow']}")
        prev_fast = ctx.indicators.get(f"ma{params['fast']}_prev")
        prev_slow = ctx.indicators.get(f"ma{params['slow']}_prev")
        if None in (fast, slow, prev_fast, prev_slow):
            return False, "", {}
        diff_now = fast - slow
        diff_prev = prev_fast - prev_slow
        if params["direction"] == "golden":
            fired = diff_prev <= 0 < diff_now
            label = "金叉"
        else:
            fired = diff_prev >= 0 > diff_now
            label = "死叉"
        message = (
            f"{ctx.name or ctx.code} MA{params['fast']}/MA{params['slow']} {label}"
            f"（{prev_fast:.2f}/{prev_slow:.2f} → {fast:.2f}/{slow:.2f}）"
        )
        return fired, message if fired else "", {"fast": fast, "slow": slow, "label": label}

    if rule_type == "macd_cross":
        dif = ctx.indicators.get("macd_dif")
        dea = ctx.indicators.get("macd_dea")
        prev_dif = ctx.indicators.get("macd_dif_prev")
        prev_dea = ctx.indicators.get("macd_dea_prev")
        if None in (dif, dea, prev_dif, prev_dea):
            return False, "", {}
        if params["direction"] == "golden":
            fired = (prev_dif - prev_dea) <= 0 < (dif - dea)
            label = "金叉"
        else:
            fired = (prev_dif - prev_dea) >= 0 > (dif - dea)
            label = "死叉"
        message = f"{ctx.name or ctx.code} MACD {label}（DIF {dif:.3f}, DEA {dea:.3f}）"
        return fired, message if fired else "", {"dif": dif, "dea": dea, "label": label}

    if rule_type == "rsi_extreme":
        key = f"rsi{params['period']}"
        value = ctx.indicators.get(key)
        if value is None or value != value:
            return False, "", {}
        threshold = params["threshold"]
        if params["direction"] == "oversold":
            fired = value <= threshold
            label = "超卖"
        else:
            fired = value >= threshold
            label = "超买"
        message = f"{ctx.name or ctx.code} RSI{params['period']} {value:.2f} {label}"
        return fired, message if fired else "", {"rsi": value, "label": label}

    if rule_type == "kdj_cross":
        k = ctx.indicators.get("kdj_k")
        d = ctx.indicators.get("kdj_d")
        prev_k = ctx.indicators.get("kdj_k_prev")
        prev_d = ctx.indicators.get("kdj_d_prev")
        if None in (k, d, prev_k, prev_d):
            return False, "", {}
        if params["direction"] == "golden":
            fired = (prev_k - prev_d) <= 0 < (k - d)
            label = "金叉"
        else:
            fired = (prev_k - prev_d) >= 0 > (k - d)
            label = "死叉"
        message = f"{ctx.name or ctx.code} KDJ {label}（K {k:.2f}, D {d:.2f}）"
        return fired, message if fired else "", {"k": k, "d": d, "label": label}

    if rule_type == "boll_break":
        close = ctx.close
        if close is None:
            return False, "", {}
        if params["band"] == "upper":
            band = ctx.indicators.get("boll_up")
            fired = band is not None and close > band
            message = f"{ctx.name or ctx.code} 突破布林上轨 {band:.2f}（收 {close:.2f}）" if fired else ""
        else:
            band = ctx.indicators.get("boll_low")
            fired = band is not None and close < band
            message = f"{ctx.name or ctx.code} 跌破布林下轨 {band:.2f}（收 {close:.2f}）" if fired else ""
        return fired, message, {"close": close, "band": band}

    if rule_type == "new_high":
        window = params["window"]
        highs = ctx.series("high")
        lows = ctx.series("low")
        if len(highs) < window + 1 or len(lows) < window + 1:
            return False, "", {}
        if params["direction"] == "high":
            prior = max(highs[-(window + 1):-1])
            fired = highs[-1] > prior
            message = f"{ctx.name or ctx.code} 创 {window} 日新高 {highs[-1]:.2f}（前高 {prior:.2f}）" if fired else ""
            snapshot = {"high": highs[-1], "prior_high": prior}
        else:
            prior = min(lows[-(window + 1):-1])
            fired = lows[-1] < prior
            message = f"{ctx.name or ctx.code} 创 {window} 日新低 {lows[-1]:.2f}（前低 {prior:.2f}）" if fired else ""
            snapshot = {"low": lows[-1], "prior_low": prior}
        return fired, message, snapshot

    if rule_type == "prediction":
        probability = ctx.prediction.get("prob_up")
        if probability is None:
            return False, "", {}
        threshold = params["threshold"]
        fired = probability >= threshold if params["direction"] == "above" else probability <= threshold
        message = (
            f"{ctx.name or ctx.code} 预测上涨概率 {probability:.2%} "
            f"（阈值 {threshold:.2%}，周期 {params['horizon']} 日）"
        )
        return fired, message if fired else "", {"prob_up": probability, "threshold": threshold}

    if rule_type == "drawdown":
        window = params["window"]
        closes = ctx.series("close")
        if len(closes) < 2:
            return False, "", {}
        recent = closes[-window:] if len(closes) >= window else closes
        peak = max(recent)
        if peak <= 0:
            return False, "", {}
        drawdown = (1.0 - recent[-1] / peak) * 100.0
        threshold = params["threshold"]
        fired = drawdown >= threshold
        message = f"{ctx.name or ctx.code} 自 {window} 日高点回撤 {drawdown:.2f}%（阈值 {threshold:.2f}%）"
        return fired, message if fired else "", {"drawdown_pct": drawdown, "threshold": threshold}

    if rule_type == "factor_score":
        score = ctx.factors.get("score")
        if score is None:
            return False, "", {}
        threshold = params["threshold"]
        fired = score >= threshold if params["direction"] == "above" else score <= threshold
        message = f"{ctx.name or ctx.code} 因子评分 {score:.3f} 触发阈值 {threshold:.3f}"
        return fired, message if fired else "", {"score": score, "threshold": threshold}

    raise DataError(f"rule_type {rule_type!r} has no evaluator")


def _dedup_key(
    rule: AlertRule, ctx: RuleContext, trigger_at: datetime
) -> str:
    """Stable identity for a triggering event.

    ``once_per_day`` collapses the whole session to a single alert per symbol,
    which is what a user almost always wants. Otherwise a cooldown bucket is
    used so a sustained condition cannot alert on every scan.
    """
    day = ctx.trade_date or trigger_at.date()
    if rule.once_per_day:
        return f"{rule.id}:{ctx.code}:{day.isoformat()}"
    if rule.cooldown_seconds and rule.cooldown_seconds > 0:
        bucket = int(trigger_at.timestamp()) // int(rule.cooldown_seconds)
        return f"{rule.id}:{ctx.code}:{day.isoformat()}:b{bucket}"
    # No dedup window configured: fall back to minute granularity to avoid
    # creating one row per scan.
    return f"{rule.id}:{ctx.code}:{day.isoformat()}:m{trigger_at:%Y%m%d%H%M}"


class AlertEngine:
    """Evaluate stored rules and persist de-duplicated triggers."""

    def __init__(self, *, respect_market_hours: bool = False) -> None:
        # Market-hours gating is opt-in because backtests and tests must be able
        # to evaluate rules at any wall-clock time.
        self.respect_market_hours = respect_market_hours

    # ------------------------------------------------------------------ scope
    @staticmethod
    def _resolve_scope(session, rule: AlertRule) -> list[str]:
        """Codes covered by a rule's scope definition."""
        scope = rule.scope_type or "watchlist"
        if scope == "codes":
            return [c for c in (rule.scope_value or "").split(",") if c.strip()]
        if scope == "index":
            from app.sync import repository as repo

            return repo.index_members(session, rule.scope_value or "000300.SH")
        if scope == "watchlist":
            name = rule.scope_value or "default"
            rows = session.execute(
                select(WatchlistItem.code).where(WatchlistItem.list_name == name)
            ).scalars().all()
            if rows:
                return list(rows)
            # Fall back to all active instruments when the watchlist is empty, so
            # a fresh install still produces something useful.
            scope = "all"
        if scope == "all":
            rows = session.execute(
                select(Instrument.code).where(
                    Instrument.status == 1, Instrument.is_index.is_(False)
                )
            ).scalars().all()
            return list(rows)
        return []

    # -------------------------------------------------------------- building
    @staticmethod
    def _build_context(session, code: str, lookback: int = 260) -> RuleContext | None:
        """Assemble a point-in-time context for one symbol."""
        from app.factors.compute import compute_factors
        from app.factors.compute import composite_score as factor_score
        from app.indicators.compute import compute_indicators
        from app.sync import repository as repo

        instrument = repo.get_instrument(session, code)
        bars = repo.get_bars(session, code, "qfq", limit=lookback)
        if not bars:
            return None

        import pandas as pd

        frame = pd.DataFrame(
            {
                "trade_date": [b.trade_date for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
                "amount": [b.amount for b in bars],
                "turn": [b.turn if b.turn is not None else float("nan") for b in bars],
            }
        )

        indicators_frame = compute_indicators(frame)
        factors_frame = compute_factors(frame)
        last = frame.iloc[-1]

        indicators: dict[str, float | None] = {}
        for column in indicators_frame.columns:
            value = indicators_frame[column].iloc[-1]
            indicators[column] = None if pd.isna(value) else float(value)
            # Previous value for crossover rules.
            if len(indicators_frame) > 1:
                prev = indicators_frame[column].iloc[-2]
                indicators[f"{column}_prev"] = None if pd.isna(prev) else float(prev)

        factors: dict[str, float | None] = {}
        for column in factors_frame.columns:
            value = factors_frame[column].iloc[-1]
            factors[column] = None if pd.isna(value) else float(value)
        score_series = factor_score(factors_frame)
        factors["score"] = None if pd.isna(score_series.iloc[-1]) else float(score_series.iloc[-1])

        # Latest stored prediction, if any.
        from app.db.models import Prediction

        prediction_row = session.execute(
            select(Prediction)
            .where(Prediction.code == code)
            .order_by(Prediction.trade_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        prediction = (
            {"prob_up": prediction_row.prob_up, "horizon": prediction_row.horizon}
            if prediction_row is not None
            else {}
        )

        history = [
            {
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
            }
            for bar in bars
        ]

        return RuleContext(
            code=code,
            name=(instrument.name if instrument else ""),
            trade_date=last["trade_date"],
            close=float(last["close"]),
            preclose=float(bars[-1].preclose) if bars[-1].preclose is not None else None,
            open=float(last["open"]),
            high=float(last["high"]),
            low=float(last["low"]),
            volume=float(last["volume"]),
            amount=float(last["amount"]),
            pct_chg=bars[-1].pct_chg,
            turn=bars[-1].turn,
            is_st=bool(bars[-1].is_st),
            indicators=indicators,
            factors=factors,
            prediction=prediction,
            history=history,
        )

    # ---------------------------------------------------------------- scanning
    def evaluate_all(
        self,
        *,
        rule_ids: list[int] | None = None,
        persist: bool = True,
        now: datetime | None = None,
    ) -> list[TriggerCandidate]:
        """Evaluate every enabled rule and persist new, de-duplicated triggers."""
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)

        if self.respect_market_hours:
            from app.config import settings

            local = (now.replace(tzinfo=timezone.utc)).astimezone(settings.tz)
            if not is_trading_time(local) or is_call_auction(local):
                return []

        candidates: list[TriggerCandidate] = []

        with session_scope() as session:
            query = select(AlertRule).where(AlertRule.enabled.is_(True))
            if rule_ids:
                query = query.where(AlertRule.id.in_(rule_ids))
            rules = list(session.execute(query).scalars())
            if not rules:
                return []

            for rule in rules:
                try:
                    params = normalize_params(rule.rule_type, rule.params_json)
                except DataError as exc:
                    log.warning("skipping malformed rule %s: %s", rule.id, exc)
                    continue

                codes = self._resolve_scope(session, rule)
                if not codes:
                    continue

                for code in codes:
                    ctx = self._build_context(session, code)
                    if ctx is None:
                        continue
                    # Scope-level filters that apply before rule logic.
                    if rule.exclude_st and ctx.is_st:
                        continue
                    if rule.min_amount and (ctx.amount or 0) < rule.min_amount:
                        continue

                    try:
                        fired, message, snapshot = evaluate_rule(
                            ctx, rule.rule_type, params
                        )
                    except Exception as exc:  # noqa: BLE001 - one rule must not stop the scan
                        log.warning("rule %s failed on %s: %s", rule.id, code, exc)
                        continue
                    if not fired:
                        continue

                    candidates.append(
                        TriggerCandidate(
                            rule_id=rule.id,
                            code=code,
                            name=ctx.name,
                            trade_date=ctx.trade_date or now.date(),
                            rule_type=rule.rule_type,
                            message=message,
                            snapshot={**snapshot, "rule": describe_rule(rule.rule_type, params)},
                            dedup_key=_dedup_key(rule, ctx, now),
                            trigger_at=now,
                        )
                    )

            if persist and candidates:
                inserted = self._persist(session, candidates)
                # Only report what was actually new, so callers can notify on the
                # delta rather than re-notifying already-known triggers.
                inserted_keys = {
                    (row.rule_id, row.dedup_key) for row in inserted
                }
                candidates = [
                    c for c in candidates
                    if (c.rule_id, c.dedup_key) in inserted_keys
                ]

        return candidates

    @staticmethod
    def _persist(session, candidates: list[TriggerCandidate]) -> list[AlertTrigger]:
        """Insert triggers, ignoring ones already recorded (idempotent)."""
        rows = [
            {
                "rule_id": c.rule_id,
                "code": c.code,
                "name": c.name,
                "trade_date": c.trade_date,
                "trigger_at": c.trigger_at,
                "rule_type": c.rule_type,
                "message": c.message,
                "snapshot_json": json.dumps(c.snapshot, default=str),
                "dedup_key": c.dedup_key,
                "status": "new",
                "notified": False,
            }
            for c in candidates
        ]

        # ``ON CONFLICT DO NOTHING`` makes re-scanning harmless. RETURNING tells us
        # which rows were genuinely new.
        stmt = (
            sqlite_insert(AlertTrigger)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["rule_id", "dedup_key"])
            .returning(AlertTrigger)
        )
        return list(session.execute(stmt).scalars())

    # ------------------------------------------------------------------ helpers
    def create_rule(
        self,
        *,
        name: str,
        rule_type: str,
        params: dict | str | None = None,
        scope_type: str = "watchlist",
        scope_value: str = "",
        cooldown_seconds: int = 0,
        once_per_day: bool = True,
        exclude_st: bool = True,
        min_amount: float = 0.0,
        enabled: bool = True,
    ) -> int:
        """Validate and store a rule; returns its id."""
        if rule_type not in RULE_TYPES:
            raise DataError(
                f"unknown rule_type {rule_type!r}; expected one of "
                f"{', '.join(sorted(RULE_TYPES))}"
            )
        normalized = normalize_params(rule_type, params)
        with session_scope() as session:
            rule = AlertRule(
                name=name,
                rule_type=rule_type,
                params_json=json.dumps(normalized),
                scope_type=scope_type,
                scope_value=scope_value,
                cooldown_seconds=cooldown_seconds,
                once_per_day=once_per_day,
                exclude_st=exclude_st,
                min_amount=min_amount,
                enabled=enabled,
            )
            session.add(rule)
            session.flush()
            return int(rule.id)

    def purge_old_triggers(self, days: int = 90) -> int:
        """Delete triggers older than ``days`` (housekeeping)."""
        from sqlalchemy import delete

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        with session_scope() as session:
            result = session.execute(
                delete(AlertTrigger).where(AlertTrigger.trigger_at < cutoff)
            )
            return int(result.rowcount or 0)
