"""隔离边界从「账号」升级为「工作区」：``owner_id`` → ``workspace_id``。

为什么不再按账号隔离
------------------
按账号隔离只能表达「我的数据谁也看不见」，表达不了「这个自选清单是我们三个人
共用的，小李只能看、小王能改」。cndb 的做法是把**工作区**当作数据的归属单位，
角色（owner/editor/viewer）挂在成员关系上。于是数据列从「属于谁」改成「属于哪个
工作区」，每个账号自动拥有一个 personal 工作区 —— 单人使用时它等价于原来的按账号
隔离，多人时可以把它分享出去。

迁移做了什么
------------
1. 建 ``workspaces`` / ``workspace_members``（全新库上 ``create_all`` 已建好，跳过）。
2. 给每个已有账号补一个 personal 工作区，并让它成为该工作区的 owner。
3. 四张表的 ``owner_id`` 重命名为 ``workspace_id``，再把「用户 id」重映射成
   「该用户的 personal 工作区 id」。``0``（未归属的历史数据）保持不变。
4. 索引跟着改名；自选股唯一约束的列名随列重命名自动更新，若检测到仍是旧列则重建。

**重映射必须一条 UPDATE 完成**：工作区 id 与用户 id 会大量重合（都是 1、2、3…），
逐个用户 ``UPDATE ... WHERE workspace_id = <uid>`` 会把上一步刚改写过的值再映射一次，
数据被搬到别人的工作区里。所以用「一次带子查询的 UPDATE」，只读原始值。

SQLite 从 3.25 起支持 ``RENAME COLUMN``，直接改列名即可，不必整表重建（batch 模式
要搬数据、重建索引，对这种只改名不改类型的改动是多余的风险）。其它方言走 Alembic
的通用 ``alter_column``。

Revision ID: 0003_workspaces
Revises: 0002_owner_columns
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_workspaces"
down_revision: str | None = "0002_owner_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (表, 旧索引名, 新索引名, 新索引列)
DATA_TABLES = (
    ("watchlist_items", None, None, None),  # 唯一约束自带索引前缀，不另建
    ("alert_rules", "ix_alert_rules_owner", "ix_alert_rules_workspace", ("workspace_id", "enabled")),
    ("alert_triggers", "ix_alert_triggers_owner", "ix_alert_triggers_workspace", ("workspace_id", "trigger_at")),
    ("backtest_runs", "ix_backtest_runs_owner", "ix_backtest_runs_workspace", ("workspace_id", "created_at")),
)

WATCHLIST_UQ = "uq_watchlist_item"
WATCHLIST_UQ_COLUMNS = ["workspace_id", "list_name", "code"]

PERSONAL_KIND = "personal"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in set(_inspector().get_table_names())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {idx["name"] for idx in _inspector().get_indexes(table) if idx.get("name")}


def _unique_constraint(table: str, name: str) -> list[str] | None:
    for row in _inspector().get_unique_constraints(table):
        if row.get("name") == name:
            return list(row.get("column_names") or [])
    return None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


# --------------------------------------------------------------------------- #
# Step 1-2: 工作区表与 personal 工作区
# --------------------------------------------------------------------------- #


def _create_workspace_tables() -> None:
    """建表（幂等）。列定义与 ORM 模型保持一致，仅作为存量库的补齐路径。"""
    if not _has_table("workspaces"):
        op.create_table(
            "workspaces",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("description", sa.String(256), nullable=False, server_default=""),
            sa.Column("kind", sa.String(16), nullable=False, server_default="shared"),
            sa.Column("owner_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        )
        op.create_index("ix_workspaces_owner", "workspaces", ["owner_id"])

    if not _has_table("workspace_members"):
        op.create_table(
            "workspace_members",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "workspace_id",
                sa.Integer(),
                sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("role", sa.String(16), nullable=False, server_default="viewer"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
        )
        op.create_index("ix_workspace_member_user", "workspace_members", ["user_id", "workspace_id"])


def _ensure_personal_workspaces() -> None:
    """给每个账号补 personal 工作区，并补上 owner 成员关系。"""
    if not _has_table("users"):
        return
    conn = op.get_bind()
    users = conn.execute(sa.text("SELECT id, username, nickname FROM users ORDER BY id")).fetchall()
    for user_id, username, nickname in users:
        existing = conn.execute(
            sa.text("SELECT id FROM workspaces WHERE owner_id = :uid AND kind = :kind LIMIT 1"),
            {"uid": user_id, "kind": PERSONAL_KIND},
        ).first()
        if existing is None:
            label = (nickname or username or "用户").strip()
            conn.execute(
                sa.text(
                    "INSERT INTO workspaces (name, description, kind, owner_id, created_at, updated_at) "
                    "VALUES (:name, '', :kind, :uid, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"name": f"{label}的空间", "kind": PERSONAL_KIND, "uid": user_id},
            )
            existing = conn.execute(
                sa.text("SELECT id FROM workspaces WHERE owner_id = :uid AND kind = :kind LIMIT 1"),
                {"uid": user_id, "kind": PERSONAL_KIND},
            ).first()
        if existing is None:  # pragma: no cover - 插入失败会立刻在上一步抛错
            continue
        workspace_id = int(existing[0])
        member = conn.execute(
            sa.text("SELECT id FROM workspace_members WHERE workspace_id = :w AND user_id = :u"),
            {"w": workspace_id, "u": user_id},
        ).first()
        if member is None:
            conn.execute(
                sa.text(
                    "INSERT INTO workspace_members (workspace_id, user_id, role, created_at, updated_at) "
                    "VALUES (:w, :u, 'owner', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"w": workspace_id, "u": user_id},
            )


# --------------------------------------------------------------------------- #
# Step 3: 列重命名 + 重映射
# --------------------------------------------------------------------------- #


def _rename_column(table: str, old: str, new: str) -> None:
    if _is_sqlite():
        # 只改列名，不重建表：SQLite 会同步更新索引与约束里的列引用。
        op.execute(sa.text(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"'))
    else:  # pragma: no cover - 项目只发布 SQLite，保留通用路径以免迁移链被方言绑死
        op.alter_column(table, old, new_column_name=new)


def _remap_to_personal_workspaces(table: str) -> None:
    """用户 id → 该用户的 personal 工作区 id，一条语句完成（避免二次映射）。"""
    op.execute(
        sa.text(
            f'UPDATE "{table}" SET "workspace_id" = ('
            "  SELECT w.id FROM workspaces w"
            '   WHERE w.kind = :kind AND w.owner_id = "workspace_id"'
            "   LIMIT 1"
            ") WHERE workspace_id != 0 AND EXISTS ("
            "  SELECT 1 FROM workspaces w"
            '   WHERE w.kind = :kind AND w.owner_id = "workspace_id"'
            ")"
        ).bindparams(kind=PERSONAL_KIND)
    )


def _rename_index(table: str, old: str | None, new: str | None, columns: tuple[str, ...] | None) -> None:
    if not old or not new or not columns:
        return
    names = _index_names(table)
    if old in names:
        op.drop_index(old, table_name=table)
        names.discard(old)
    if new not in names:
        op.create_index(new, table, list(columns))


def _ensure_watchlist_unique() -> None:
    existing = _unique_constraint("watchlist_items", WATCHLIST_UQ)
    if existing == WATCHLIST_UQ_COLUMNS:
        return
    if existing is None:
        with op.batch_alter_table("watchlist_items", schema=None) as batch:
            batch.create_unique_constraint(WATCHLIST_UQ, WATCHLIST_UQ_COLUMNS)
        return
    # SQLite 的 RENAME COLUMN 通常已经改写了约束定义；走到这里说明没改写成功。
    with op.batch_alter_table("watchlist_items", schema=None) as batch:
        batch.drop_constraint(WATCHLIST_UQ, type_="unique")
        batch.create_unique_constraint(WATCHLIST_UQ, WATCHLIST_UQ_COLUMNS)


def upgrade() -> None:
    _create_workspace_tables()
    _ensure_personal_workspaces()

    for table, old_index, new_index, columns in DATA_TABLES:
        if _has_column(table, "owner_id") and not _has_column(table, "workspace_id"):
            _rename_column(table, "owner_id", "workspace_id")
        if _has_column(table, "workspace_id"):
            _remap_to_personal_workspaces(table)
            _rename_index(table, old_index, new_index, columns)

    if _has_column("watchlist_items", "workspace_id"):
        _ensure_watchlist_unique()


def downgrade() -> None:
    """退回「按账号隔离」：先把工作区映射回 owner，再改回列名。"""
    conn = op.get_bind()
    for table, _old_index, _new_index, _columns in DATA_TABLES:
        if not _has_column(table, "workspace_id"):
            continue
        # 单条 UPDATE：工作区 → 该工作区的 owner_id（没有 owner 的保持 0）。
        conn.execute(
            sa.text(
                f'UPDATE "{table}" SET "workspace_id" = COALESCE(('
                "  SELECT w.owner_id FROM workspaces w WHERE w.id = "
                f'  "{table}"."workspace_id"'
                "), 0) WHERE workspace_id != 0"
            )
        )

    if _unique_constraint("watchlist_items", WATCHLIST_UQ) == WATCHLIST_UQ_COLUMNS:
        with op.batch_alter_table("watchlist_items", schema=None) as batch:
            batch.drop_constraint(WATCHLIST_UQ, type_="unique")
            batch.create_unique_constraint(WATCHLIST_UQ, ["owner_id", "list_name", "code"])

    for table, old_index, new_index, columns in DATA_TABLES:
        if not _has_column(table, "workspace_id"):
            continue
        _rename_index(table, new_index, old_index, columns)
        if _is_sqlite():
            op.execute(sa.text(f'ALTER TABLE "{table}" RENAME COLUMN "workspace_id" TO "owner_id"'))
        else:  # pragma: no cover
            op.alter_column(table, "workspace_id", new_column_name="owner_id")
