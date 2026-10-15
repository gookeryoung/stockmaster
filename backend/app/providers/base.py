"""Unified data-provider contract.

Every provider returns the same normalised structures so the sync layer is
source-agnostic and any source can be swapped, compared, or degraded away.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date

# Canonical adjust modes used across the app.
ADJUST_RAW = "raw"
ADJUST_QFQ = "qfq"
ADJUST_HFQ = "hfq"
VALID_ADJUSTS = (ADJUST_RAW, ADJUST_QFQ, ADJUST_HFQ)


@dataclass(slots=True)
class Bar:
    """One daily bar, already normalised.

    ``code`` is canonical (``600519.SH``). Values are in yuan/share and shares
    unless the provider documents otherwise. ``None`` means "provider did not
    supply this field" and must never be silently coerced to zero.
    """

    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    preclose: float | None = None
    pct_chg: float | None = None
    turn: float | None = None
    pe_ttm: float | None = None
    pb_mrq: float | None = None
    is_st: bool = False
    adjust: str = ADJUST_RAW
    source: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class InstrumentInfo:
    """Security master row."""

    code: str
    name: str = ""
    market: str = ""
    board: str = ""
    is_index: bool = False
    is_st: bool = False
    status: int = 1
    list_date: date | None = None
    delist_date: date | None = None
    source: str = ""


@dataclass(slots=True)
class Snapshot:
    """Intraday quote snapshot (used by the alert poller)."""

    code: str
    name: str = ""
    price: float | None = None
    preclose: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    amount: float | None = None
    pct_chg: float | None = None
    turnover: float | None = None
    quote_time: str = ""
    source: str = ""
    extras: dict = field(default_factory=dict)


class BaseProvider(ABC):
    """Common provider interface.

    Implementations must be resilient: raise :class:`ProviderError` subclasses on
    failure rather than returning partial/empty frames, so the registry can fail
    over deliberately.
    """

    name: str = "base"
    #: Providers that need an explicit login must set this True.
    requires_login: bool = False

    @abstractmethod
    def fetch_daily(
        self,
        code: str,
        start: date,
        end: date,
        adjust: str = ADJUST_RAW,
    ) -> list[Bar]:
        """Daily bars in ``[start, end]``, ascending by date."""

    @abstractmethod
    def fetch_calendar(self, start: date, end: date) -> list[date]:
        """Open trading days in ``[start, end]``."""

    @abstractmethod
    def list_instruments(self) -> list[InstrumentInfo]:
        """Full security master."""

    # ---------------------------------------------------------------- optional
    def fetch_snapshot(self, codes: list[str] | None = None) -> list[Snapshot]:
        """Intraday snapshots. Providers without support raise."""
        raise NotImplementedError(f"{self.name} does not support snapshots")

    def fetch_index_daily(self, code: str, start: date, end: date) -> list[Bar]:
        """Index daily bars. Defaults to the equity path."""
        return self.fetch_daily(code, start, end, ADJUST_RAW)

    def health_check(self) -> bool:
        """Cheap liveness probe. Conservative default: assume healthy."""
        return True

    def close(self) -> None:  # noqa: B027 - intentional no-op default hook
        """Release resources (sockets, sessions). Must be idempotent."""
