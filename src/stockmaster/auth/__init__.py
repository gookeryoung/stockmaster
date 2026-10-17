"""Accounts and authentication.

Layout
------
``passwords``  PBKDF2 hashing / password policy
``tokens``     self-issued HS256 JWT (issue, verify, secret handling)
``wechat``     WeChat OAuth client (open / mp / mock)
``service``    user, session, OAuth-state and audit logic (DB-facing)
``deps``       FastAPI dependencies (``current_user`` / ``require_admin``)
``gate``       middleware that enforces login when ``SM_AUTH_ENABLED=true``
"""

__all__ = ["deps", "gate", "passwords", "service", "tokens", "wechat"]
