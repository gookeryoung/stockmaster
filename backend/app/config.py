"""Application settings.

All configuration is environment-driven (prefix ``SM_``) with sane defaults so the
app runs out of the box on a single machine. See ``.env.example``.
"""

from __future__ import annotations

import os
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
    # 气隙/CI 模式：不实例化真实数据源，所有取数调用立即失败（不发起任何网络
    # 请求）。生产默认关闭；测试用它把「数据源不可达」的路径变成确定且毫秒级的。
    provider_offline: bool = False
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

    # --- Auth ---
    # When false the API stays open (single-machine mode) but the account pages
    # still work, so accounts can be provisioned before the switch is flipped.
    auth_enabled: bool = False
    # HS256 signing key. Left empty it is generated once and persisted under the
    # data directory, so tokens survive restarts without mandatory configuration.
    auth_secret_key: str = ""
    auth_token_ttl_minutes: int = 60 * 24 * 7
    auth_allow_register: bool = True
    auth_first_user_admin: bool = True
    auth_oauth_state_ttl_seconds: int = 600
    auth_ticket_ttl_seconds: int = 120
    # Public origin of the SPA (used for post-login redirects). Empty = derive from
    # the request, which is correct when the API and UI share an origin; set it to
    # the Vite dev server origin during local development.
    auth_frontend_base: str = ""
    auth_max_failed_logins: int = 8
    auth_failed_login_window: int = 900
    # Bootstrap administrator, created on startup when it does not exist yet.
    auth_admin_username: str = ""
    auth_admin_password: str = ""
    # --- User data isolation ---
    # 自选股 / 预警规则 / 预警记录 / 回测历史是否按 ``owner_id`` 隔离。
    # 默认 False = 沿用「所有人共享同一份数据」的旧语义（单机升级后行为完全不变）；
    # 置 True 后 API 只读写本人数据，``owner_id = 0`` 仍代表「未归属」的历史数据。
    # 列与迁移始终存在，切开关不需要重新迁移，可随时回退。
    data_isolation: bool = False
    # --- Maintenance ---
    # 启动时自动把库结构带到最新（create_all + Alembic）。生产若要求「先显式迁移
    # 再启动」，把它设为 false，启动时只告警不升级，由 deploy.sh 调 `db upgrade`。
    db_auto_migrate: bool = True
    # 自动备份保留份数（本地 backups 目录，0 表示不清理）
    backup_keep: int = 7
    # 备份是否包含 <数据目录>/.auth_secret。包含才能「整体回到过去」（否则恢复后
    # 所有人被踢下线、已签发的微信 state 全部失效），但归档因此等同凭据，需妥善保管。
    backup_include_secret: bool = True

    # --- WeChat login ---
    # open = 网站应用扫码 (open.weixin.qq.com), mp = 公众号网页授权,
    # mock = 本地模拟（无凭据时演练流程）, off = 关闭。
    wechat_login_mode: str = "open"
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    # Must match the callback configured on the WeChat platform exactly.
    wechat_redirect_uri: str = ""
    wechat_timeout: float = 10.0

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

    @field_validator("wechat_login_mode")
    @classmethod
    def _validate_wechat_mode(cls, v: str) -> str:
        mode = v.strip().lower()
        if mode not in {"open", "mp", "mock", "off"}:
            raise ValueError("wechat_login_mode must be one of: open, mp, mock, off")
        return mode

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
    def backups_path(self) -> Path:
        """Local backup directory (inside the data volume, so volume backups carry it)."""
        return self.data_path / "backups"

    @property
    def provider_priority(self) -> list[str]:
        return [p.strip() for p in self.provider_order.split(",") if p.strip()]

    @property
    def horizons(self) -> list[int]:
        return [int(h) for h in self.predict_horizons.split(",") if h.strip()]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def auth_secret_path(self) -> Path:
        """Where the auto-generated signing key lives (git-ignored, 0600)."""
        return self.data_path / ".auth_secret"

    @property
    def wechat_mode(self) -> str:
        """Effective WeChat login mode after credential checks.

        ``open``/``mp`` need both an AppID and a secret; without them the flow is
        reported as ``off`` instead of failing at redirect time. ``mock`` never
        needs credentials because it never calls WeChat.
        """
        mode = self.wechat_login_mode
        if mode == "mock":
            return "mock"
        if mode in {"open", "mp"} and self.wechat_app_id and self.wechat_app_secret:
            return mode
        return "off"

    @property
    def wechat_enabled(self) -> bool:
        return self.wechat_mode != "off"

    @property
    def console_encoding_override(self) -> str:
        """Explicit console encoding override (``SM_CONSOLE_ENCODING``).

        Read straight from the environment rather than through the ``SM_``-prefixed
        settings model, because the console helper must work before settings are
        resolved. Surfaced here purely for diagnostics.
        """
        return os.environ.get("SM_CONSOLE_ENCODING", "").strip()

    def ensure_dirs(self) -> None:
        """Create the data directories if missing."""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.backups_path.mkdir(parents=True, exist_ok=True)


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
