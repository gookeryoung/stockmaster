"""Alert rule definitions.

Rules are declarative and evaluated against a point-in-time snapshot of a symbol
(its latest bar, indicators, factors and prediction). Keeping them data-driven
means the API layer can create them from JSON without executing user code, and
every trigger can be explained by the parameters that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.errors import DataError

__all__ = [
    "RuleContext",
    "RULE_TYPES",
    "normalize_params",
    "describe_rule",
]


@dataclass
class RuleContext:
    """Everything a rule may inspect for one symbol on one day.

    All values are point-in-time. ``history`` holds the symbol's recent bars
    (ascending) for rules that need a series rather than a single value.
    """

    code: str
    name: str = ""
    trade_date: date | None = None
    close: float | None = None
    preclose: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    amount: float | None = None
    pct_chg: float | None = None
    turn: float | None = None
    is_st: bool = False
    # Derived analytics, when available for this symbol.
    indicators: dict[str, float | None] = field(default_factory=dict)
    factors: dict[str, float | None] = field(default_factory=dict)
    prediction: dict[str, Any] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def value(self, field_name: str) -> float | None:
        """Look up a named value across the flat bar fields and analytics."""
        if hasattr(self, field_name):
            raw = getattr(self, field_name)
            return float(raw) if isinstance(raw, (int, float)) else None
        for source in (self.indicators, self.factors, self.prediction):
            if field_name in source:
                raw = source[field_name]
                if isinstance(raw, (int, float)):
                    return float(raw)
        return None

    def series(self, field_name: str) -> list[float]:
        """Recent history of a named field, ascending, NaNs dropped."""
        out: list[float] = []
        for row in self.history:
            raw = row.get(field_name)
            if isinstance(raw, (int, float)):
                out.append(float(raw))
        return out


#: Every supported rule type with the parameters it accepts and what it means.
#:
#: ``params`` documents the schema so the API can validate and the frontend can
#: render an editor without duplicating this knowledge.
RULE_TYPES: dict[str, dict[str, Any]] = {
    "price_cross": {
        "description": "价格穿越阈值（收盘价上穿或下穿）",
        "params": {
            "direction": {"type": "str", "choices": ["above", "below"], "default": "above"},
            "threshold": {"type": "float", "required": True},
        },
    },
    "pct_change": {
        "description": "当日涨跌幅超过阈值",
        "params": {
            "direction": {"type": "str", "choices": ["above", "below"], "default": "above"},
            "threshold": {"type": "float", "required": True},
        },
    },
    "price_change_pct": {
        "description": "相对参考价（默认昨收）的涨跌幅阈值",
        "params": {
            "reference": {"type": "str", "choices": ["preclose", "close"], "default": "preclose"},
            "direction": {"type": "str", "choices": ["above", "below"], "default": "above"},
            "threshold": {"type": "float", "required": True},
        },
    },
    "volume_ratio": {
        "description": "量比（相对前 N 日均量）超过阈值",
        "params": {
            "direction": {"type": "str", "choices": ["above", "below"], "default": "above"},
            "threshold": {"type": "float", "default": 2.0},
            "window": {"type": "int", "default": 5},
        },
    },
    "turnover": {
        "description": "换手率超过阈值（百分数）",
        "params": {
            "direction": {"type": "str", "choices": ["above", "below"], "default": "above"},
            "threshold": {"type": "float", "default": 5.0},
        },
    },
    "ma_cross": {
        "description": "两条均线金叉/死叉",
        "params": {
            "fast": {"type": "int", "default": 5},
            "slow": {"type": "int", "default": 20},
            "direction": {"type": "str", "choices": ["golden", "death"], "default": "golden"},
        },
    },
    "macd_cross": {
        "description": "MACD 金叉/死叉（DIF 与 DEA）",
        "params": {
            "direction": {"type": "str", "choices": ["golden", "death"], "default": "golden"},
        },
    },
    "rsi_extreme": {
        "description": "RSI 超买或超卖",
        "params": {
            "period": {"type": "int", "choices": [6, 12, 24], "default": 6},
            "direction": {"type": "str", "choices": ["overbought", "oversold"], "default": "oversold"},
            "threshold": {"type": "float", "default": 30.0},
        },
    },
    "kdj_cross": {
        "description": "KDJ 金叉/死叉",
        "params": {
            "direction": {"type": "str", "choices": ["golden", "death"], "default": "golden"},
        },
    },
    "boll_break": {
        "description": "突破布林带上轨/跌破下轨",
        "params": {
            "band": {"type": "str", "choices": ["upper", "lower"], "default": "upper"},
        },
    },
    "new_high": {
        "description": "创 N 日新高/新低",
        "params": {
            "window": {"type": "int", "default": 60},
            "direction": {"type": "str", "choices": ["high", "low"], "default": "high"},
        },
    },
    "prediction": {
        "description": "模型预测上涨概率超过阈值",
        "params": {
            "horizon": {"type": "int", "default": 1},
            "direction": {"type": "str", "choices": ["above", "below"], "default": "above"},
            "threshold": {"type": "float", "default": 0.6},
        },
    },
    "drawdown": {
        "description": "自 N 日高点回撤超过阈值（百分数，正数表示回撤幅度）",
        "params": {
            "window": {"type": "int", "default": 60},
            "threshold": {"type": "float", "default": 10.0},
        },
    },
    "factor_score": {
        "description": "综合因子评分超过阈值（标准化分数）",
        "params": {
            "direction": {"type": "str", "choices": ["above", "below"], "default": "above"},
            "threshold": {"type": "float", "default": 1.0},
        },
    },
}


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] is None:
        raise DataError(f"rule parameter {key!r} is required")
    return params[key]


def normalize_params(rule_type: str, raw: dict | str | None) -> dict:
    """Validate and default a rule's parameters.

    Raises:
        DataError: for an unknown rule type or a missing required parameter.
    """
    if rule_type not in RULE_TYPES:
        raise DataError(
            f"unknown rule_type {rule_type!r}; expected one of "
            f"{', '.join(sorted(RULE_TYPES))}"
        )

    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise DataError(f"rule params is not valid JSON: {exc}") from exc
    params = dict(raw or {})

    schema = RULE_TYPES[rule_type]["params"]
    out: dict[str, Any] = {}
    for key, spec in schema.items():
        if key in params and params[key] is not None:
            value = params[key]
            # Coerce numeric strings, which is what JSON forms produce.
            if spec["type"] == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise DataError(f"parameter {key!r} must be an integer") from exc
            elif spec["type"] == "float":
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise DataError(f"parameter {key!r} must be a number") from exc
            elif spec["type"] == "str":
                value = str(value)
            if "choices" in spec and value not in spec["choices"]:
                raise DataError(
                    f"parameter {key!r} must be one of {spec['choices']}, got {value!r}"
                )
            out[key] = value
        elif "default" in spec:
            out[key] = spec["default"]
        elif spec.get("required"):
            raise DataError(f"rule parameter {key!r} is required for {rule_type!r}")

    # Reject unknown keys so typos surface instead of being silently ignored.
    unknown = set(params) - set(schema)
    if unknown:
        raise DataError(
            f"unknown parameters for {rule_type!r}: {', '.join(sorted(unknown))}"
        )
    return out


def describe_rule(rule_type: str, params: dict) -> str:
    """Human-readable summary, so a trigger can explain itself."""
    normalized = normalize_params(rule_type, params)
    parts = [f"{key}={value}" for key, value in normalized.items()]
    return f"{rule_type}({', '.join(parts)})"
