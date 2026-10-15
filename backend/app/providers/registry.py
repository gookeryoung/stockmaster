"""Provider registry: priority order, circuit breaking and failover.

The sync layer talks only to this object, so "which source served this" and
"what happens when a source dies" are decided in one place. Each health state is
process-local; a provider that fails ``circuit_threshold`` times in a row is
skipped until ``circuit_cooldown`` elapses, then allowed a single trial request.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date

from app.config import settings
from app.core.errors import ProviderUnavailable
from app.core.logging import get_logger
from app.providers.base import (
    ADJUST_RAW,
    Bar,
    BaseProvider,
    InstrumentInfo,
    Snapshot,
)

log = get_logger(__name__)


@dataclass
class ProviderState:
    """Circuit-breaker bookkeeping for a single provider."""

    provider: BaseProvider
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_error: str = ""
    successes: int = 0
    failures: int = 0
    #: Number of requests this provider has actually served (used for reporting).
    served: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        """True while the breaker is tripped."""
        if self.opened_at is None:
            return False
        # Once the cooldown elapses the breaker is half-open, allowing a trial.
        return time.monotonic() - self.opened_at < settings.provider_circuit_cooldown

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.last_error = ""
        self.successes += 1
        self.served += 1

    def record_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        self.failures += 1
        self.last_error = error
        if self.consecutive_failures >= settings.provider_circuit_threshold:
            self.opened_at = time.monotonic()
            log.warning(
                "provider %s circuit opened after %d consecutive failures: %s",
                self.provider.name,
                self.consecutive_failures,
                error,
            )

    def snapshot(self) -> dict:
        return {
            "name": self.provider.name,
            "circuit_open": self.is_open,
            "consecutive_failures": self.consecutive_failures,
            "successes": self.successes,
            "failures": self.failures,
            "served": self.served,
            "last_error": self.last_error,
        }


class ProviderRegistry:
    """Ordered, fault-tolerant facade over the configured providers."""

    def __init__(self, providers: list[BaseProvider] | None = None, *, order: list[str] | None = None) -> None:
        self._lock = threading.RLock()
        if providers is None:
            providers = _default_providers()
        self._states: dict[str, ProviderState] = {p.name: ProviderState(provider=p) for p in providers}
        self._order: list[str] = order or settings.provider_priority
        # Any provider not named in the order is appended so nothing is lost.
        for name in self._states:
            if name not in self._order:
                self._order.append(name)
        self._order = [n for n in self._order if n in self._states]

    # ------------------------------------------------------------------ state
    @property
    def order(self) -> list[str]:
        return list(self._order)

    def states(self) -> list[dict]:
        with self._lock:
            return [self._states[n].snapshot() for n in self._order]

    def get(self, name: str) -> BaseProvider:
        """Direct access to a provider by name (used by tests and diagnostics)."""
        if name not in self._states:
            raise ProviderUnavailable(f"unknown provider: {name}")
        return self._states[name].provider

    def available_order(self) -> list[str]:
        """Provider names usable right now, in priority order."""
        with self._lock:
            return [n for n in self._order if not self._states[n].is_open]

    # ----------------------------------------------------------------- runner
    def _dispatch(self, action: str, *args, want: type | tuple[type, ...] = object, **kwargs):
        """Try each healthy provider in order, returning the first success."""
        errors: list[str] = []
        candidates = self.available_order()
        if not candidates:
            # Everything is circuit-broken: try anyway rather than hard-failing,
            # so a recovered-but-cooling provider can still serve traffic.
            candidates = list(self._order)
            log.warning("all providers circuit-open; attempting trial requests")

        for name in candidates:
            state = self._states[name]
            provider = state.provider
            method = getattr(provider, action, None)
            if method is None:
                errors.append(f"{name}: unsupported {action}")
                continue
            # Capability gap, not a failure: skip without touching provider health.
            supported = getattr(provider, "supports", None)
            if supported is not None and args and not supported(args[0]):
                errors.append(f"{name}: does not support {args[0]}")
                continue
            try:
                result = method(*args, **kwargs)
            except NotImplementedError as exc:
                # Not supported by this provider is not a provider failure.
                errors.append(f"{name}: {exc}")
                continue
            except Exception as exc:
                state.record_failure(f"{type(exc).__name__}: {exc}")
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                log.warning("provider %s failed %s: %s", name, action, exc)
                continue
            if want is not object and not isinstance(result, want):
                state.record_failure(f"bad result type {type(result).__name__}")
                errors.append(f"{name}: bad result type {type(result).__name__}")
                continue
            state.record_success()
            return result, name

        detail = "; ".join(errors) if errors else "no providers configured"
        raise ProviderUnavailable(f"all providers failed for {action}: {detail}")

    # -------------------------------------------------------------- interface
    def fetch_daily(
        self,
        code: str,
        start: date,
        end: date,
        adjust: str = ADJUST_RAW,
    ) -> list[Bar]:
        bars, _source = self._dispatch("fetch_daily", code, start, end, adjust, want=list)
        return bars

    def fetch_daily_with_source(
        self,
        code: str,
        start: date,
        end: date,
        adjust: str = ADJUST_RAW,
    ) -> tuple[list[Bar], str]:
        """Like :meth:`fetch_daily` but also reports the serving provider."""
        bars, source = self._dispatch("fetch_daily", code, start, end, adjust, want=list)
        return bars, source

    def fetch_index_daily_with_source(self, code: str, start: date, end: date) -> tuple[list[Bar], str]:
        """Index bars, reporting which provider served them.

        Indices must go through ``fetch_index_daily`` rather than ``fetch_daily``:
        the backup providers resolve a *stock* symbol on the equity endpoint, so
        index codes fail there even though the same providers serve indices
        correctly through their index endpoint.
        """
        bars, source = self._dispatch("fetch_index_daily", code, start, end, want=list)
        return bars, source

    def fetch_index_daily(self, code: str, start: date, end: date) -> list[Bar]:
        bars, _source = self.fetch_index_daily_with_source(code, start, end)
        return bars

    def fetch_calendar(self, start: date, end: date) -> list[date]:
        days, _source = self._dispatch("fetch_calendar", start, end, want=list)
        return days

    def list_instruments(self) -> list[InstrumentInfo]:
        items, _source = self._dispatch("list_instruments", want=list)
        return items

    def fetch_snapshot(self, codes: list[str] | None = None) -> list[Snapshot]:
        snaps, _source = self._dispatch("fetch_snapshot", codes, want=list)
        return snaps

    # ------------------------------------------------------------- lifecycle
    def health(self) -> dict[str, bool]:
        """Actively probe every provider (slow; for the settings page)."""
        out: dict[str, bool] = {}
        for name in self._order:
            provider = self._states[name].provider
            try:
                ok = bool(provider.health_check())
            except Exception as exc:
                log.debug("health check raised for %s: %s", name, exc)
                ok = False
            with self._lock:
                if ok:
                    self._states[name].record_success()
                else:
                    self._states[name].record_failure("health check failed")
            out[name] = ok
        return out

    def close(self) -> None:
        """Close every provider, releasing sockets."""
        for name in self._order:
            try:
                self._states[name].provider.close()
            except Exception as exc:
                log.debug("closing provider %s raised: %s", name, exc)


def _default_providers() -> list[BaseProvider]:
    """Instantiate the built-in providers, tolerating missing optional deps."""
    providers: list[BaseProvider] = []
    try:
        from app.providers.baostock_provider import BaostockProvider

        providers.append(BaostockProvider())
    except Exception as exc:
        log.warning("baostock provider unavailable: %s", exc)

    try:
        from app.providers.tencent_provider import TencentProvider

        providers.append(TencentProvider())
    except Exception as exc:
        log.warning("tencent provider unavailable: %s", exc)

    try:
        from app.providers.sina_provider import SinaProvider

        providers.append(SinaProvider())
    except Exception as exc:
        log.warning("sina provider unavailable: %s", exc)

    if not providers:
        raise ProviderUnavailable("no data providers could be initialised")
    return providers


_registry: ProviderRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ProviderRegistry:
    """Process-wide registry singleton."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ProviderRegistry()
    return _registry


def reset_registry() -> None:
    """Drop the singleton (used by tests)."""
    global _registry  # noqa: PLW0603
    with _registry_lock:
        if _registry is not None:
            _registry.close()
        _registry = None
