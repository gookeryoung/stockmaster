"""Data provider layer: unified contract, concrete sources, fault-tolerant registry."""

from app.providers.base import (
    ADJUST_HFQ,
    ADJUST_QFQ,
    ADJUST_RAW,
    VALID_ADJUSTS,
    Bar,
    BaseProvider,
    InstrumentInfo,
    Snapshot,
)
from app.providers.registry import (
    ProviderRegistry,
    ProviderState,
    get_registry,
    reset_registry,
)

__all__ = [
    "ADJUST_HFQ",
    "ADJUST_QFQ",
    "ADJUST_RAW",
    "VALID_ADJUSTS",
    "Bar",
    "BaseProvider",
    "InstrumentInfo",
    "ProviderRegistry",
    "ProviderState",
    "Snapshot",
    "get_registry",
    "reset_registry",
]
