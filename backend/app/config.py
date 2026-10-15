"""Application settings.

All configuration is environment-driven (prefix ``SM_``) with sane defaults so the
app runs out of the box on a single machine. See ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root = .../backend/app/config.py -> parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Environment-backed settings (prefix ``SM_``)."""

    model_config = SettingsConfigDict(
        env_prefix="SM_",
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 8770
    log_level: str = "INFO"

    # --- Storage ---
    data_dir: Path = Path("data")
    db_filename: str = "stockmaster.db"

    # --- Providers ---
    provider_order: str = "baostock,tencent,sina"
    provider_timeout: float = 30.0
    provider_max_retries: int = 3
    provider_circuit_threshold: int = 3
    provider_circuit_cooldown: float = 300.0
    sync_workers: int = 4
    sync_request_delay: float = 0.05

    # --- Analysis / prediction ---
    predict_horizons: str = "1,5,20"
    test_fraction: float = 0.2
    random_seed: int = 42

    # --- Alerts ---
    alert_poll_seconds: int = 60
    alert_webhook_url: str = ""

    # --- Scheduler ---
    scheduler_enabled: bool = True
    timezone: str = "Asia/Shanghai"

    # ------------------------------------------------------------------ helpers
    @field_validator("test_fraction")
    @classmethod
    def _validate_test_fraction(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("test_fraction must be between 0 and 1 (exclusive)")
        return v

    @field_validator("sync_workers")
    @classmethod
    def _validate_workers(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sync_workers must be >= 1")
        return v

    @property
    def data_path(self) -> Path:
        """Absolute data directory (created on demand)."""
        p = self.data_dir
        if not p.is_absolute():
            p = REPO_ROOT / p
        return p

    @property
    def db_path(self) -> Path:
        """Absolute SQLite database path."""
        return self.data_path / self.db_filename

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def models_dir(self) -> Path:
        return self.data_path / "models"

    @property
    def provider_priority(self) -> list[str]:
        return [p.strip() for p in self.provider_order.split(",") if p.strip()]

    @property
    def horizons(self) -> list[int]:
        return [int(h) for h in self.predict_horizons.split(",") if h.strip()]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def ensure_dirs(self) -> None:
        """Create the data directories if missing."""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()


class _SettingsProxy:
    """Transparent proxy that always resolves the *current* cached settings.

    Other modules do ``from app.config import settings`` and then read attributes
    such as ``settings.db_path``. Binding the concrete ``Settings`` instance at
    import time would freeze configuration for the process lifetime, so a later
    ``get_settings.cache_clear()`` (used by tests and by runtime reconfiguration)
    would silently have no effect on those modules. Proxying keeps the convenient
    ``settings.x`` syntax while always delegating to the live instance.
    """

    __slots__ = ()

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)

    def __setattr__(self, name: str, value) -> None:
        # Allow tests to override individual fields on the live instance.
        setattr(get_settings(), name, value)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SettingsProxy({get_settings()!r})"


settings = _SettingsProxy()
