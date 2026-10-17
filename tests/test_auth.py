"""Authentication tests: password hashing, JWTs, the login gate, WeChat login.

The suite runs against the real assembled app and a temporary SQLite database, so
middleware ordering, dependency wiring and ORM behaviour are covered together.
WeChat is exercised through ``SM_WECHAT_LOGIN_MODE=mock``, which reuses the same
state/ticket code path as production without any network access.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from stockmaster.plugins.auth.services import passwords, service, tokens

STRONG = "Passw0rd123"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App with the login gate ON, WeChat in mock mode, empty database."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"auth-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "mock")
    monkeypatch.setenv("SM_AUTH_MAX_FAILED_LOGINS", "0")

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    tokens.reset_secret_cache()
    service.reset_throttle()

    from stockmaster.main import app

    with TestClient(app) as test_client:
        yield test_client

    service.reset_throttle()
    session_mod.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def open_client(tmp_path, monkeypatch):
    """Same app with the gate OFF, i.e. single-machine mode."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"open-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "false")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "off")

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    tokens.reset_secret_cache()

    from stockmaster.main import app

    with TestClient(app) as test_client:
        yield test_client

    session_mod.reset_engine()
    get_settings.cache_clear()


def register(client: TestClient, username: str = "boss", password: str = STRONG) -> dict:
    response = client.post("/api/v1/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wechat_login(client: TestClient, name: str = "alice", **params) -> dict:
    """Walk the mock OAuth flow and return the exchanged token payload."""
    url = client.get("/api/v1/auth/wechat/url", params={"redirect": "/predict", **params})
    assert url.status_code == 200, url.text
    callback = client.get(
        "/api/v1/auth/wechat/callback",
        params={"code": f"mock:{name}", "state": url.json()["state"]},
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    assert "/auth/callback" in location
    ticket = location.split("ticket=")[1].split("&")[0]
    exchanged = client.post("/api/v1/auth/ticket", json={"ticket": ticket})
    assert exchanged.status_code == 200, exchanged.text
    return exchanged.json()


# --------------------------------------------------------------------------- #
# Password hashing and tokens (pure units)
# --------------------------------------------------------------------------- #


def test_password_hash_roundtrip():
    encoded = passwords.hash_password("correct horse battery 9")
    assert encoded.startswith("pbkdf2_sha256$")
    assert "correct horse" not in encoded
    assert passwords.verify_password("correct horse battery 9", encoded)
    assert not passwords.verify_password("wrong", encoded)
    # Two hashes of the same password must differ (random salt).
    assert encoded != passwords.hash_password("correct horse battery 9")


def test_password_verify_rejects_malformed_hash():
    assert not passwords.verify_password("x", "")
    assert not passwords.verify_password("x", "plain-text")
    assert not passwords.verify_password("x", "pbkdf2_sha256$abc$c2FsdA==$aGFzaA==")
    assert not passwords.verify_password("x", "md5$1$c2FsdA==$aGFzaA==")


def test_password_policy():
    with pytest.raises(passwords.WeakPasswordError):
        passwords.validate_password("short1")
    with pytest.raises(passwords.WeakPasswordError):
        passwords.validate_password("alllettersonly")
    with pytest.raises(passwords.WeakPasswordError):
        passwords.validate_password(" Passw0rd123")
    passwords.validate_password("Passw0rd123")


def test_token_roundtrip_and_tampering():
    raw, expires_at = tokens.issue_access_token(user_id=7, username="u", role="admin", token_version=3)
    payload = tokens.decode(raw)
    assert payload.user_id == 7
    assert payload.role == "admin"
    assert payload.token_version == 3
    assert expires_at.tzinfo is None  # stored naive UTC, like the rest of the schema

    header, body, signature = raw.split(".")
    # Re-signed payload with a different subject must not validate.
    forged = f"{header}.{body[:-4]}abcd.{signature}"
    with pytest.raises(tokens.TokenError):
        tokens.decode(forged)

    # alg=none forgery.
    import base64
    import json

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    none_token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'exp': 9999999999})}.x"
    with pytest.raises(tokens.TokenError) as excinfo:
        tokens.decode(none_token)
    assert excinfo.value.reason == "bad_algorithm"


def test_expired_token_rejected():
    raw, _ = tokens.issue_access_token(user_id=1, username="u", role="user", token_version=1, ttl_minutes=-5)
    with pytest.raises(tokens.TokenError) as excinfo:
        tokens.decode(raw)
    assert excinfo.value.reason == "expired"


def test_signing_secret_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_AUTH_SECRET_KEY", "")
    from stockmaster.config import get_settings

    get_settings.cache_clear()
    tokens.reset_secret_cache()

    first = tokens.signing_secret()
    tokens.reset_secret_cache()
    assert tokens.signing_secret() == first
    assert (tmp_path / ".auth_secret").exists()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Gate and session lifecycle
# --------------------------------------------------------------------------- #


def test_gate_blocks_api_but_keeps_public_paths_open(client):
    assert client.get("/api/v1/system/status").status_code == 401
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/auth/config").status_code == 200
    assert client.get("/api/v1/auth/config").json()["auth_enabled"] is True


def test_first_user_becomes_admin_and_can_read_me(client):
    payload = register(client)
    assert payload["user"]["role"] == "admin"
    assert payload["user"]["has_password"] is True

    me = client.get("/api/v1/auth/me", headers=auth_header(payload["token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "boss"
    assert "password_hash" not in me.json()


def test_login_and_wrong_password(client):
    register(client)
    ok = client.post("/api/v1/auth/login", json={"username": "boss", "password": STRONG})
    assert ok.status_code == 200
    assert ok.json()["token"]

    # Login also works with the email once one is set.
    token = ok.json()["token"]
    client.patch("/api/v1/auth/me", headers=auth_header(token), json={"email": "boss@example.com"})
    by_email = client.post("/api/v1/auth/login", json={"username": "BOSS@example.com", "password": STRONG})
    assert by_email.status_code == 200

    bad = client.post("/api/v1/auth/login", json={"username": "boss", "password": "nope-wrong-1"})
    assert bad.status_code == 401
    # Same generic message for unknown users: no account enumeration.
    unknown = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "nope-wrong-1"})
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == bad.json()["detail"]


def test_logout_revokes_existing_token(client):
    token = register(client)["token"]
    assert client.get("/api/v1/auth/me", headers=auth_header(token)).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=auth_header(token)).status_code == 204
    assert client.get("/api/v1/auth/me", headers=auth_header(token)).status_code == 401
    # The password still works.
    assert client.post("/api/v1/auth/login", json={"username": "boss", "password": STRONG}).status_code == 200


def test_password_change_rotates_token(client):
    token = register(client)["token"]
    changed = client.post(
        "/api/v1/auth/me/password",
        headers=auth_header(token),
        json={"old_password": STRONG, "new_password": "Passw0rd456"},
    )
    assert changed.status_code == 200
    new_token = changed.json()["token"]
    assert client.get("/api/v1/auth/me", headers=auth_header(token)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=auth_header(new_token)).status_code == 200

    wrong_old = client.post(
        "/api/v1/auth/me/password",
        headers=auth_header(new_token),
        json={"old_password": "bogus", "new_password": "Passw0rd789"},
    )
    assert wrong_old.status_code == 400


def test_disabled_account_cannot_use_existing_token(client):
    admin = register(client)["token"]
    created = client.post("/api/v1/users", headers=auth_header(admin), json={"username": "bob", "password": STRONG})
    bob_id = created.json()["id"]
    bob_token = client.post("/api/v1/auth/login", json={"username": "bob", "password": STRONG}).json()["token"]
    assert client.get("/api/v1/auth/me", headers=auth_header(bob_token)).status_code == 200

    client.patch(f"/api/v1/users/{bob_id}", headers=auth_header(admin), json={"status": "disabled"})
    assert client.get("/api/v1/auth/me", headers=auth_header(bob_token)).status_code == 401
    relogin = client.post("/api/v1/auth/login", json={"username": "bob", "password": STRONG})
    assert relogin.status_code == 403


def test_failed_login_is_throttled(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"throttle-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_AUTH_MAX_FAILED_LOGINS", "3")

    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    tokens.reset_secret_cache()
    service.reset_throttle()

    from stockmaster.main import app

    with TestClient(app) as client:
        register(client)
        for _ in range(3):
            client.post("/api/v1/auth/login", json={"username": "boss", "password": "nope-wrong-1"})
        blocked = client.post("/api/v1/auth/login", json={"username": "boss", "password": STRONG})
        assert blocked.status_code == 429
        service.reset_throttle()
        assert client.post("/api/v1/auth/login", json={"username": "boss", "password": STRONG}).status_code == 200

    session_mod.reset_engine()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Registration policy
# --------------------------------------------------------------------------- #


def test_registration_can_be_disabled(client, monkeypatch):
    from stockmaster.config import get_settings

    monkeypatch.setenv("SM_AUTH_ALLOW_REGISTER", "false")
    get_settings.cache_clear()
    response = client.post("/api/v1/auth/register", json={"username": "eve", "password": STRONG})
    assert response.status_code == 403


def test_weak_password_and_duplicate_registration_rejected(client):
    weak = client.post("/api/v1/auth/register", json={"username": "weak", "password": "abc"})
    assert weak.status_code == 422

    register(client)
    duplicate = client.post("/api/v1/auth/register", json={"username": "boss", "password": STRONG})
    assert duplicate.status_code == 409


# --------------------------------------------------------------------------- #
# WeChat login
# --------------------------------------------------------------------------- #


def test_wechat_mock_login_creates_account_and_reuses_it(client):
    register(client)  # first account bootstraps the admin; WeChat logins are plain users
    first = wechat_login(client, "alice")
    assert first["user"]["role"] == "user"
    assert first["user"]["has_password"] is False
    assert first["user"]["identities"][0]["provider"] == "wechat"

    second = wechat_login(client, "alice")
    assert second["user"]["id"] == first["user"]["id"]
    assert second["user"]["username"] == first["user"]["username"]


def test_wechat_authorize_url_uses_state_and_redirect(client):
    response = client.get("/api/v1/auth/wechat/url", params={"redirect": "/backtest"})
    body = response.json()
    assert body["mode"] == "mock"
    assert len(body["state"]) >= 16
    assert "state=" in body["url"]


def test_wechat_state_cannot_be_replayed(client):
    url = client.get("/api/v1/auth/wechat/url").json()
    params = {"code": "mock:alice", "state": url["state"]}
    assert client.get("/api/v1/auth/wechat/callback", params=params, follow_redirects=False).status_code == 302
    replay = client.get("/api/v1/auth/wechat/callback", params=params, follow_redirects=False)
    assert replay.status_code == 302
    assert "error=" in replay.headers["location"]


def test_wechat_ticket_is_single_use(client):
    url = client.get("/api/v1/auth/wechat/url").json()
    callback = client.get(
        "/api/v1/auth/wechat/callback",
        params={"code": "mock:bob", "state": url["state"]},
        follow_redirects=False,
    )
    ticket = callback.headers["location"].split("ticket=")[1].split("&")[0]
    assert client.post("/api/v1/auth/ticket", json={"ticket": ticket}).status_code == 200
    assert client.post("/api/v1/auth/ticket", json={"ticket": ticket}).status_code == 400


def test_wechat_binding_to_existing_account_and_unbind_guard(client):
    token = register(client)["token"]
    headers = auth_header(token)

    url = client.get("/api/v1/auth/wechat/url", headers=headers, params={"bind": "true"}).json()
    callback = client.get(
        "/api/v1/auth/wechat/callback",
        params={"code": "mock:boss", "state": url["state"]},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    ticket = callback.headers["location"].split("ticket=")[1].split("&")[0]
    assert client.post("/api/v1/auth/ticket", json={"ticket": ticket}).status_code == 200

    identities = client.get("/api/v1/auth/me/identities", headers=headers).json()
    assert len(identities) == 1
    # The account still has a password, so unbinding is allowed.
    identity_id = identities[0]["id"]
    assert client.delete(f"/api/v1/auth/me/identities/{identity_id}", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me/identities", headers=headers).json() == []


def test_wechat_only_account_cannot_unbind_last_method(client):
    payload = wechat_login(client, "carol")
    headers = auth_header(payload["token"])
    identity_id = payload["user"]["identities"][0]["id"]
    blocked = client.delete(f"/api/v1/auth/me/identities/{identity_id}", headers=headers)
    assert blocked.status_code == 400
    assert "唯一" in blocked.json()["detail"]


def test_wechat_disabled_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"off-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_WECHAT_LOGIN_MODE", "open")
    monkeypatch.setenv("SM_WECHAT_APP_ID", "")
    monkeypatch.setenv("SM_WECHAT_APP_SECRET", "")

    from stockmaster.config import get_settings

    get_settings.cache_clear()
    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    tokens.reset_secret_cache()

    from stockmaster.main import app

    with TestClient(app) as client:
        config = client.get("/api/v1/auth/config").json()
        assert config["wechat_enabled"] is False
        assert config["wechat_mode"] == "off"
        response = client.get("/api/v1/auth/wechat/url")
        assert response.status_code == 400
        assert "SM_WECHAT_APP_ID" in response.json()["detail"]

    session_mod.reset_engine()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# User management
# --------------------------------------------------------------------------- #


def _two_accounts(client: TestClient) -> tuple[str, str]:
    admin = register(client)["token"]
    wechat_login(client, "member")
    return admin, "member"


def test_non_admin_is_forbidden_on_user_management(client):
    register(client)
    member = wechat_login(client, "member")["token"]
    headers = auth_header(member)

    assert client.get("/api/v1/users", headers=headers).status_code == 403
    assert client.post("/api/v1/users", headers=headers, json={"username": "x1", "password": STRONG}).status_code == 403


def test_admin_user_crud(client):
    admin = register(client)["token"]
    headers = auth_header(admin)

    created = client.post(
        "/api/v1/users",
        headers=headers,
        json={"username": "bob", "password": STRONG, "nickname": "鲍勃", "role": "user"},
    )
    assert created.status_code == 201
    bob = created.json()
    assert bob["nickname"] == "鲍勃"
    assert bob["has_password"] is True

    renamed = client.patch(f"/api/v1/users/{bob['id']}", headers=headers, json={"nickname": "Bob"})
    assert renamed.status_code == 200
    assert renamed.json()["nickname"] == "Bob"

    promoted = client.patch(f"/api/v1/users/{bob['id']}", headers=headers, json={"role": "admin"})
    assert promoted.json()["role"] == "admin"

    reset = client.post(f"/api/v1/users/{bob['id']}/password", headers=headers, json={"new_password": "Passw0rd999"})
    assert reset.status_code == 204
    assert client.post("/api/v1/auth/login", json={"username": "bob", "password": "Passw0rd999"}).status_code == 200

    search = client.get("/api/v1/users", headers=headers, params={"q": "bob"}).json()
    assert search["total"] == 1

    deleted = client.delete(f"/api/v1/users/{bob['id']}", headers=headers)
    assert deleted.status_code == 204
    assert client.get("/api/v1/users", headers=headers).json()["total"] == 1


def test_admin_cannot_lock_themselves_out(client):
    admin = register(client)["token"]
    headers = auth_header(admin)
    me_id = 1

    assert client.patch(f"/api/v1/users/{me_id}", headers=headers, json={"role": "user"}).status_code == 400
    assert client.delete(f"/api/v1/users/{me_id}", headers=headers).status_code == 400


def test_last_admin_cannot_be_demoted(client):
    admin = register(client)["token"]
    headers = auth_header(admin)
    # Self-protection blocks demotion; a second admin can demote the first.
    second = client.post(
        "/api/v1/users", headers=headers, json={"username": "root2", "password": STRONG, "role": "admin"}
    ).json()
    second_token = client.post("/api/v1/auth/login", json={"username": "root2", "password": STRONG}).json()["token"]
    # Demoting admin #1 by admin #2 is fine (one admin remains).
    assert client.patch("/api/v1/users/1", headers=auth_header(second_token), json={"role": "user"}).status_code == 200
    # Now root2 is the only admin and cannot demote itself.
    assert (
        client.patch(
            f"/api/v1/users/{second['id']}", headers=auth_header(second_token), json={"role": "user"}
        ).status_code
        == 400
    )


def test_audit_log_records_events(client):
    token = register(client)["token"]
    client.post("/api/v1/auth/login", json={"username": "boss", "password": "wrong-pass-1"})
    client.post("/api/v1/auth/login", json={"username": "boss", "password": STRONG})
    logs = client.get("/api/v1/users/audit-logs", headers=auth_header(token), params={"limit": 20}).json()
    actions = {row["action"] for row in logs}
    assert "login.success" in actions
    assert "login.failed" in actions


# --------------------------------------------------------------------------- #
# Single-machine mode (gate off)
# --------------------------------------------------------------------------- #


def test_gate_off_keeps_api_open_and_allows_guest_admin(open_client):
    assert open_client.get("/api/v1/system/status").status_code != 401
    assert open_client.get("/api/v1/auth/me").status_code == 401

    # Accounts can be provisioned before the gate is switched on.
    created = open_client.post("/api/v1/users", json={"username": "boss", "password": STRONG})
    assert created.status_code == 201
    assert created.json()["role"] == "admin"

    # A token works even while the gate is off.
    logged_in = open_client.post("/api/v1/auth/login", json={"username": "boss", "password": STRONG})
    assert logged_in.status_code == 200
    me = open_client.get("/api/v1/auth/me", headers=auth_header(logged_in.json()["token"]))
    assert me.json()["username"] == "boss"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_user_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"cli-{uuid.uuid4().hex}.db")

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import session as session_mod

    session_mod.reset_engine()

    from stockmaster.cli import main

    assert main(["user", "add", "--username", "ops", "--role", "admin"]) == 0
    out = capsys.readouterr().out
    assert "已创建账号 ops" in out
    assert "初始密码" in out

    assert main(["user", "list"]) == 0
    assert "ops" in capsys.readouterr().out

    assert main(["user", "passwd", "--username", "ops", "--password", "Passw0rd789"]) == 0
    assert "已重置" in capsys.readouterr().out

    assert main(["user", "role", "--username", "ops", "--status", "disabled"]) == 0
    assert "disabled" in capsys.readouterr().out

    assert main(["user", "delete", "--username", "ops"]) == 1
    assert "至少需要保留一名启用的管理员" in capsys.readouterr().err

    session_mod.reset_engine()
    get_settings.cache_clear()
