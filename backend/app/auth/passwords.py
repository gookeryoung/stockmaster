"""Password hashing.

PBKDF2-HMAC-SHA256 from the standard library rather than bcrypt/argon2: it needs
no extra wheel, which keeps the offline install path (and the Docker build) free
of compiled dependencies, while still being a defensible choice at a high
iteration count. The parameters are stored inside the hash string, so the cost
can be raised later without invalidating existing passwords.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class WeakPasswordError(ValueError):
    """Password fails the minimum policy."""


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def validate_password(password: str) -> None:
    """Enforce the password policy shared by registration, reset and change."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(f"密码最多 {MAX_PASSWORD_LENGTH} 位")
    if password.strip() != password:
        raise WeakPasswordError("密码不能以空白字符开头或结尾")
    if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
        raise WeakPasswordError("密码需同时包含字母和数字")


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt>$<hash>``."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check. Malformed hashes are treated as a mismatch."""
    if not encoded:
        return False
    try:
        algorithm, raw_iterations, salt, expected = encoded.split("$", 3)
        iterations = int(raw_iterations)
    except (ValueError, AttributeError):
        return False
    if algorithm != ALGORITHM or iterations < 1:
        return False
    try:
        salt_bytes = _b64decode(salt)
        expected_bytes = _b64decode(expected)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    return hmac.compare_digest(digest, expected_bytes)


def needs_rehash(encoded: str) -> bool:
    """True when a stored hash uses weaker parameters than the current policy."""
    try:
        algorithm, raw_iterations, _salt, _digest = encoded.split("$", 3)
        return algorithm != ALGORITHM or int(raw_iterations) < ITERATIONS
    except (ValueError, AttributeError):
        return bool(encoded)
