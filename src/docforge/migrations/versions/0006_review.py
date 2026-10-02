"""Review: reviewers, corrections, signed reviews, and the model's reply kept with each extraction.

Revision ID: 0006
Revises: 0005
"""

import os
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOW_DOWNGRADE = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"
_UUID = postgresql.UUID(as_uuid=True)
_APPEND_ONLY = ("corrections", "reviews")


def _tenant() -> sa.Column[Any]:
    return sa.Column(
        "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )


def _created_at(name: str = "created_at") -> sa.Column[Any]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _version_of(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["document_version_id", "tenant_id"],
        ["document_versions.id", "document_versions.tenant_id"],
        name=f"fk_{table}_version_tenant",
        ondelete="RESTRICT",
    )


def _reviewer_of(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["reviewer_id", "tenant_id"],
        ["reviewers.id", "reviewers.tenant_id"],
        name=f"fk_{table}_reviewer_tenant",
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    # What the model returned, merged across pages: corrections are applied to it and it is
    # read again. Null for extractions made before this migration.
    op.add_column("extractions", sa.Column("raw", postgresql.JSONB, nullable=True))

    op.create_table(
        "reviewers",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("pin_hash", sa.Text, nullable=False),
        sa.Column("failed_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.UniqueConstraint("id", "tenant_id", name="uq_reviewers_id_tenant"),
        sa.CheckConstraint("email = lower(email)", name="ck_reviewers_email_lower"),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_reviewers_failed_attempts"),
    )
    op.create_index("uq_reviewers_tenant_email", "reviewers", ["tenant_id", "email"], unique=True)

    op.create_table(
        "corrections",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant(),
        sa.Column("document_version_id", _UUID, nullable=False),
        sa.Column("reviewer_id", _UUID, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("old_text", sa.Text, nullable=True),
        sa.Column("new_text", sa.Text, nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        _created_at(),
        _version_of("corrections"),
        _reviewer_of("corrections"),
        sa.CheckConstraint("length(reason) > 0", name="ck_corrections_reason"),
    )
    op.create_index("ix_corrections_version", "corrections", ["document_version_id", "created_at"])

    op.create_table(
        "reviews",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant(),
        sa.Column("document_version_id", _UUID, nullable=False),
        sa.Column("reviewer_id", _UUID, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("meaning", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("override_reason", sa.Text, nullable=True),
        sa.Column("record_sha256", sa.CHAR(64), nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),  # the signed record and any draft
        _created_at("signed_at"),
        _version_of("reviews"),
        _reviewer_of("reviews"),
        sa.UniqueConstraint("document_version_id", name="uq_reviews_version"),
        sa.CheckConstraint("outcome IN ('approved', 'rejected')", name="ck_reviews_outcome"),
        sa.CheckConstraint("length(meaning) > 0 AND length(reason) > 0", name="ck_reviews_text"),
        sa.CheckConstraint("record_sha256 ~ '^[0-9a-f]{64}$'", name="ck_reviews_sha256"),
    )
    for table in _APPEND_ONLY:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
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
        for table in (*_APPEND_ONLY, "reviewers"):
            query = sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")  # noqa: S608 - fixed names
            if op.get_bind().execute(query).scalar():
                raise RuntimeError(
                    f"{table} has records and this downgrade would discard them; "
                    f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
                )
    op.drop_table("reviews")
    op.drop_table("corrections")
    op.drop_table("reviewers")
    op.drop_column("extractions", "raw")
