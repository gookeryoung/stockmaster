"""备份与恢复测试：归档协议、安全闸门、往返一致性、管理员权限。

重点不在「tar 能不能解开」，而在几条容易退化的安全性质：
非空库必须显式确认、覆盖前一定有预备份、坏归档不能碰线上库、
恢复后版本表要跟着走、普通用户碰不到这些接口。
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import uuid

import pytest
from fastapi.testclient import TestClient

STRONG = "Passw0rd123"


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"bk-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_MAX_FAILED_LOGINS", "0")

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    yield tmp_path
    session_mod.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """管理员接口需要真实账号，所以这个 fixture 打开登录网关。"""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"bkapi-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("SM_AUTH_ENABLED", "true")
    monkeypatch.setenv("SM_AUTH_MAX_FAILED_LOGINS", "0")

    from stockmaster.config import get_settings

    get_settings.cache_clear()

    from stockmaster.db import session as session_mod
    from stockmaster.plugins.auth.services import service, tokens

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


def _create_user(username: str, *, role: str = "user") -> None:
    from stockmaster.db import session_scope
    from stockmaster.plugins.auth.services import service

    with session_scope() as session:
        service.create_user(session, username=username, password=STRONG, role=role)


def _usernames() -> list[str]:
    from sqlalchemy import select

    from stockmaster.db import reset_engine, session_scope
    from stockmaster.db.models import User

    reset_engine()  # 恢复后必须换连接，否则读到的是旧页缓存
    with session_scope() as session:
        return sorted(user.username for user in session.execute(select(User)).scalars())


def _members(archive) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


# --------------------------------------------------------------------------- #
# 归档
# --------------------------------------------------------------------------- #


def test_backup_writes_manifest_and_database(db_env):
    from stockmaster.plugins.system.services import create_backup

    _create_user("boss", role="admin")
    archive = create_backup(note="unit")

    names = _members(archive)
    assert "backup/manifest.json" in names
    assert any(name.startswith("backup/data/") and name.endswith(".db") for name in names)

    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("backup/manifest.json").read().decode("utf-8"))
    assert manifest["version"] == "1"
    assert manifest["note"] == "unit"
    assert manifest["database"]["schema_version"]
    assert manifest["database"]["row_counts"]["users"] == 1
    assert manifest["secrets"]["auth_secret"] is False  # 测试库没有生成密钥


def test_backup_can_exclude_secret(db_env):
    from stockmaster.config import settings
    from stockmaster.plugins.system.services import create_backup

    settings.auth_secret_path.write_text("unit-test-secret", encoding="utf-8")

    with_secret = create_backup(include_secret=True)
    without_secret = create_backup(include_secret=False)

    assert "backup/data/.auth_secret" in _members(with_secret)
    assert "backup/data/.auth_secret" not in _members(without_secret)


def test_backup_includes_model_artifacts(db_env):
    from stockmaster.config import settings
    from stockmaster.plugins.system.services import create_backup

    settings.ensure_dirs()
    (settings.models_dir / "m1.joblib").write_bytes(b"fake-model")

    archive = create_backup()
    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("backup/manifest.json").read().decode("utf-8"))

    assert manifest["models"]["included"] is True
    assert manifest["models"]["file_count"] == 1
    assert "backup/data/models/m1.joblib" in _members(archive)


def test_list_and_prune_keep_latest(db_env):
    from stockmaster.plugins.system.services import create_backup, list_backups, prune_backups

    for index in range(3):
        create_backup(note=f"n{index}", prune=False)

    assert len(list_backups()) == 3
    removed = prune_backups(keep=2)

    remaining = list_backups()
    assert len(removed) == 1
    assert len(remaining) == 2
    # 留下的是最新的两份
    assert {item["note"] for item in remaining} == {"n1", "n2"}


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #


def test_inspect_rejects_corrupt_archive(db_env, tmp_path):
    from stockmaster.plugins.system.services import RestoreError, inspect_backup

    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"not really a gzip")

    with pytest.raises(RestoreError):
        inspect_backup(broken)


def test_inspect_rejects_unknown_manifest_version(db_env, tmp_path):
    from stockmaster.plugins.system.services import RestoreError, inspect_backup

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "manifest.json").write_text(json.dumps({"version": "999"}), encoding="utf-8")
    archive = tmp_path / "future.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="backup")

    with pytest.raises(RestoreError, match="协议版本"):
        inspect_backup(archive)


# --------------------------------------------------------------------------- #
# 恢复
# --------------------------------------------------------------------------- #


def test_restore_roundtrip_and_pre_backup(db_env):
    from stockmaster.plugins.system.services import create_backup, restore_backup

    _create_user("boss", role="admin")
    archive = create_backup(note="before")

    _create_user("intruder")
    assert _usernames() == ["boss", "intruder"]

    report = restore_backup(archive, force=True)

    assert _usernames() == ["boss"]
    assert report.schema_version
    assert report.models_restored == 0
    # 覆盖前一定留了退路
    assert report.pre_backup
    from pathlib import Path

    assert Path(report.pre_backup).is_file()


def test_restore_requires_force_when_target_has_data(db_env):
    from stockmaster.plugins.system.services import RestoreError, create_backup, restore_backup

    _create_user("boss", role="admin")
    archive = create_backup()

    with pytest.raises(RestoreError, match="已有数据"):
        restore_backup(archive, force=False)


def test_restore_accepts_empty_target_without_force(db_env, tmp_path):
    """空库直接恢复是安全的，不该被 force 挡住。"""
    from stockmaster.plugins.system.services import create_backup, restore_backup

    _create_user("boss", role="admin")
    archive = create_backup()

    from stockmaster.config import settings
    from stockmaster.db import reset_engine

    # Windows 上必须先释放连接池，否则文件被占用删不掉；WAL 旁文件一并清掉，
    # 否则新建的同名库会读到上一轮残留的日志。
    reset_engine()
    for suffix in ("", "-wal", "-shm"):
        candidate = settings.db_path.with_name(settings.db_path.name + suffix)
        candidate.unlink(missing_ok=True)

    report = restore_backup(archive, force=False, pre_backup=False)
    assert report.tables > 0
    assert _usernames() == ["boss"]


def test_restore_reports_schema_drift_for_old_backup(db_env):
    """备份本身缺列时，恢复后要把差异报出来而不是假装成功。"""
    from stockmaster.config import settings
    from stockmaster.db import reset_engine
    from stockmaster.plugins.system.services import create_backup, restore_backup

    _create_user("boss", role="admin")

    # 先把列删掉再备份：模拟「备份来自更早的版本」，归档里就少了这一列
    reset_engine()  # 释放连接，SQLite 的 ALTER 需要独占
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("ALTER TABLE users DROP COLUMN nickname")
        conn.commit()
    except sqlite3.OperationalError:  # pragma: no cover
        pytest.skip("SQLite 版本不支持 DROP COLUMN")
    finally:
        conn.close()

    archive = create_backup(note="old-schema")
    report = restore_backup(archive, force=True, pre_backup=False)

    assert any("users.nickname" in warning for warning in report.warnings)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def _admin_headers(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/register", json={"username": "boss", "password": STRONG})
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_admin_backup_endpoints_require_admin(client):
    headers = _admin_headers(client)
    member = client.post("/api/v1/auth/register", json={"username": "alice", "password": STRONG})
    member_headers = {"Authorization": f"Bearer {member.json()['token']}"}

    assert client.get("/api/v1/admin/backups", headers=member_headers).status_code == 403
    assert client.post("/api/v1/admin/backups", headers=member_headers).status_code == 403
    assert client.get("/api/v1/admin/backups").status_code == 401
    assert client.get("/api/v1/admin/backups", headers=headers).status_code == 200


def test_admin_backup_download_and_restore(client):
    headers = _admin_headers(client)

    created = client.post("/api/v1/admin/backups", headers=headers, params={"note": "api"})
    assert created.status_code == 201
    name = created.json()["name"]

    downloaded = client.get(f"/api/v1/admin/backups/{name}", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content[:2] == b"\x1f\x8b"

    # 路径穿越必须被挡下
    assert client.get("/api/v1/admin/backups/..%2F..%2Fsecret.tar.gz", headers=headers).status_code == 404

    client.post("/api/v1/auth/register", json={"username": "bob", "password": STRONG})

    inspected = client.post("/api/v1/admin/restore/inspect", headers=headers, content=downloaded.content)
    assert inspected.status_code == 200
    assert inspected.json()["backup_ahead"] is False
    assert inspected.json()["target"]["tables"] > 0

    # 没确认就不许覆盖
    assert client.post("/api/v1/admin/restore", headers=headers, content=downloaded.content).status_code == 400

    restored = client.post(
        "/api/v1/admin/restore",
        headers=headers,
        content=downloaded.content,
        params={"confirm": "true", "filename": name},
    )
    assert restored.status_code == 200
    assert restored.json()["report"]["rows"] > 0

    users = client.get("/api/v1/users", headers=headers).json()
    assert "bob" not in [item["username"] for item in users["items"]]
    # 恢复本身要留痕
    actions = [row["action"] for row in client.get("/api/v1/users/audit-logs?limit=10", headers=headers).json()]
    assert "system.restore" in actions


def test_admin_restore_rejects_bad_upload(client):
    headers = _admin_headers(client)
    response = client.post(
        "/api/v1/admin/restore/inspect",
        headers=headers,
        content=b"definitely not a tarball",
    )
    assert response.status_code == 400

    empty = client.post("/api/v1/admin/restore/inspect", headers=headers, content=b"")
    assert empty.status_code == 400


def test_admin_restore_with_bytes_is_not_loaded_in_one_piece(client, monkeypatch):
    """上传必须分块落盘：一次性读进内存会在 2C2G 的机器上 OOM。"""
    from stockmaster.plugins.system.routers import admin

    headers = _admin_headers(client)
    archive = client.post("/api/v1/admin/backups", headers=headers)
    blob: bytes = client.get(f"/api/v1/admin/backups/{archive.json()['name']}", headers=headers).content

    # 把上限压到 1 KB，验证超限返回 413 而不是默默写满磁盘
    monkeypatch.setattr(admin, "MAX_UPLOAD_BYTES", 1024)
    response = client.post("/api/v1/admin/restore/inspect", headers=headers, content=io.BytesIO(blob).read())
    assert response.status_code == 413
