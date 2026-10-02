"""Assessments and cross-document matches.

Revision ID: 0004
Revises: 0003
"""

import os
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOW_DOWNGRADE = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"
_UUID = postgresql.UUID(as_uuid=True)
_TABLES = ("assessments", "matches")


def _common() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def _version(name: str) -> sa.Column[Any]:
    return sa.Column(
        name, _UUID, sa.ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "assessments",
        *_common(),
        _version("document_version_id"),
        sa.Column("decision", sa.Text, nullable=False),
        sa.UniqueConstraint("document_version_id", name="uq_assessments_version"),
        sa.CheckConstraint("decision IN ('accept', 'review')", name="ck_assessments_decision"),
    )
    op.create_table(
        "matches",
        *_common(),
        _version("invoice_version_id"),
        _version("order_version_id"),
        sa.Column("decision", sa.Text, nullable=False),
        sa.UniqueConstraint("invoice_version_id", "order_version_id", name="uq_matches_pair"),
        sa.CheckConstraint("decision IN ('match', 'mismatch')", name="ck_matches_decision"),
    )
    op.create_index("ix_matches_order_version_id", "matches", ["order_version_id"])
    for table in _TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        # Results of checks are records too: append-only, like extractions.
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION docforge_forbid_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION docforge_forbid_change()"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_append_only")
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {table}_no_truncate")


def downgrade() -> None:
    if os.environ.get(_ALLOW_DOWNGRADE) != "1":
        for table in _TABLES:
            query = sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")  # noqa: S608 - fixed names
            if op.get_bind().execute(query).scalar():
                raise RuntimeError(
                    f"{table} has records and this downgrade would discard them; "
                    f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
                )
    for table in reversed(_TABLES):
        op.drop_table(table)
