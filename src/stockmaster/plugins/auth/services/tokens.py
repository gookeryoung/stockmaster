"""Self-issued JWT (HS256) implemented with the standard library.

Why not PyJWT: the project installs offline on a 2C2G box and pulls nothing that
needs compilation; HS256 is a single HMAC over two base64url segments, so the
whole algorithm fits in this file and stays audit-able. The implementation is
deliberately strict — the algorithm is pinned, segments are re-encoded before
comparison, and ``exp``/``iat`` are always enforced.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from stockmaster.config import settings
from stockmaster.core.logging import get_logger

log = get_logger(__name__)

ALGORITHM = "HS256"
# Tolerated clock skew when validating time claims.
LEEWAY_SECONDS = 60


class TokenError(Exception):
    """Raised for any invalid/expired token. ``reason`` is machine-readable."""

    def __init__(self, reason: str = "invalid") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TokenPayload:
    user_id: int
    username: str
    role: str
    token_version: int
    issued_at: datetime
    expires_at: datetime
    token_id: str


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _generate_secret(path: Path) -> str:
    """Create the signing key once and keep it out of the repository."""
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    # Best effort: some filesystems (and NTFS via Git Bash) reject POSIX modes.
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    log.warning(
        "SM_AUTH_SECRET_KEY 未配置，已在 %s 自动生成签名密钥；"
        "多实例部署或多环境下请显式配置同一个密钥，否则令牌互不通用。",
        path,
    )
    return secret


@lru_cache(maxsize=4)
def _resolve_secret(configured: str, secret_path: str) -> str:
    if configured:
        return configured
    path = Path(secret_path)
    if path.exists():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    return _generate_secret(path)


def signing_secret() -> str:
    """Configured key, or the persisted auto-generated one."""
    return _resolve_secret(settings.auth_secret_key.strip(), str(settings.auth_secret_path))


def reset_secret_cache() -> None:
    """Drop the cached key (tests switch the data directory)."""
    _resolve_secret.cache_clear()


def _sign(signing_input: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def encode(payload: dict, secret: str | None = None) -> str:
    """Serialise and sign an arbitrary claim set."""
    header = {"alg": ALGORITHM, "typ": "JWT"}
    segments = [
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments)
    return f"{signing_input}.{_sign(signing_input, secret or signing_secret())}"


def issue_access_token(
    *,
    user_id: int,
    username: str,
    role: str,
    token_version: int,
    ttl_minutes: int | None = None,
) -> tuple[str, datetime]:
    """Mint an access token and report its expiry (naive UTC, as stored)."""
    ttl = ttl_minutes if ttl_minutes is not None else settings.auth_token_ttl_minutes
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=ttl)
    payload = {
        "sub": str(user_id),
        "usr": username,
        "role": role,
        "tv": token_version,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = encode(payload)
    return token, expires.replace(tzinfo=None)


def decode(token: str, *, verify_exp: bool = True) -> TokenPayload:
    """Validate signature and claims, then return a typed payload."""
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed")
    header_segment, payload_segment, signature = parts

    try:
        header = json.loads(_b64url_decode(header_segment))
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed") from exc
    if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
        # Pinning the algorithm is what blocks the classic "alg: none" forgery.
        raise TokenError("bad_algorithm")

    expected = _sign(f"{header_segment}.{payload_segment}", signing_secret())
    if not hmac.compare_digest(expected, signature):
        raise TokenError("bad_signature")

    try:
        claims = json.loads(_b64url_decode(payload_segment))
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed") from exc
    if not isinstance(claims, dict):
        raise TokenError("malformed")

    now = int(time.time())
    try:
        expires = int(claims["exp"])
        issued = int(claims.get("iat", 0))
        user_id = int(claims["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("missing_claim") from exc

    if issued and issued > now + LEEWAY_SECONDS:
        raise TokenError("iat_in_future")
    if verify_exp and expires + LEEWAY_SECONDS < now:
        raise TokenError("expired")

    return TokenPayload(
        user_id=user_id,
        username=str(claims.get("usr", "")),
        role=str(claims.get("role", "user")),
        token_version=int(claims.get("tv", 1)),
        issued_at=datetime.fromtimestamp(issued, UTC),
        expires_at=datetime.fromtimestamp(expires, UTC),
        token_id=str(claims.get("jti", "")),
    )


def random_state(nbytes: int = 24) -> str:
    """URL-safe opaque value for OAuth ``state`` and login tickets."""
    return secrets.token_urlsafe(nbytes)
