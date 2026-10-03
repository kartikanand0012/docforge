"""Credentials: API keys for systems, sessions for people.

Revision ID: 0007
Revises: 0006

Both store a SHA-256 of the secret part only: a token is shown once, when it is made. The
secret is 32 random bytes, so a fast hash is enough; a slow one protects guessable secrets.
"""

import os
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOW_DOWNGRADE = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"
_UUID = postgresql.UUID(as_uuid=True)
_TABLES = ("sessions", "api_keys")


def _common() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("prefix", sa.CHAR(12), nullable=False),
        sa.Column("digest", sa.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "api_keys",
        *_common(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("prefix", name="uq_api_keys_prefix"),
        sa.CheckConstraint("role IN ('integrator', 'reviewer', 'admin')", name="ck_api_keys_role"),
        sa.CheckConstraint("prefix ~ '^[0-9a-f]{12}$'", name="ck_api_keys_prefix"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_api_keys_digest"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_api_keys_name"),
    )
    op.create_table(
        "sessions",
        *_common(),
        sa.Column("reviewer_id", _UUID, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("prefix", name="uq_sessions_prefix"),
        sa.ForeignKeyConstraint(
            ["reviewer_id", "tenant_id"],
            ["reviewers.id", "reviewers.tenant_id"],
            name="fk_sessions_reviewer_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("prefix ~ '^[0-9a-f]{12}$'", name="ck_sessions_prefix"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_sessions_digest"),
        sa.CheckConstraint("expires_at > created_at", name="ck_sessions_expiry"),
    )
    for table in _TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    op.create_index("ix_sessions_reviewer", "sessions", ["reviewer_id", "tenant_id"])


def downgrade() -> None:
    if os.environ.get(_ALLOW_DOWNGRADE) != "1":
        for table in _TABLES:
            query = sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")  # noqa: S608 - fixed names
            if op.get_bind().execute(query).scalar():
                raise RuntimeError(
                    f"{table} has records and this downgrade would discard them; "
                    f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
                )
    for table in _TABLES:
        op.drop_table(table)
