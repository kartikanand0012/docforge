"""Core tables: tenants, documents, document_versions; pgvector extension.

Revision ID: 0001
Revises:
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    # Needs a role allowed to create extensions (the Compose superuser; rds_superuser on RDS).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "tenants",
        _id(),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        _created_at(),
    )

    op.create_table(
        "documents",
        _id(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("doc_type", sa.Text),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="received"),
        _created_at(),
        # The content hash is the idempotency key per tenant (architecture §3, Ingest).
        sa.UniqueConstraint("tenant_id", "sha256", name="uq_documents_tenant_sha256"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256_hex"),
    )

    op.create_table(
        "document_versions",
        _id(),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("parser_version", sa.Text),
        sa.Column("schema_version", sa.Text),
        sa.Column("prompt_version", sa.Text),
        sa.Column("model_id", sa.Text),
        _created_at(),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_doc_version"),
        sa.CheckConstraint("version_no > 0", name="ck_document_versions_version_positive"),
    )


def downgrade() -> None:
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("tenants")
    # The vector extension is left installed: other database objects may depend on it.
