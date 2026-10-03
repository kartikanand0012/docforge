"""Search chunks (text, embedding, full-text vector) and the certificate batch lookup.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
DIMENSIONS = 768


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("document_id", _UUID, nullable=False),
        sa.Column("document_version_id", _UUID, nullable=False),
        sa.Column("chunk_no", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("page", sa.Integer, nullable=False),
        sa.Column("block_ids", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding_model", sa.Text, nullable=False),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=False),
        # 'simple': codes such as batch numbers and GSTINs are kept whole, not stemmed. The
        # text is indexed twice, as written and with punctuation as spaces, because the parser
        # keeps NVM/26-27/32001 as one path-like word and a question may name its parts.
        sa.Column(
            "tsv",
            postgresql.TSVECTOR,
            sa.Computed(
                "to_tsvector('simple', text || ' ' || "
                "regexp_replace(text, '[^0-9A-Za-z]+', ' ', 'g'))",
                persisted=True,
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["document_versions.id", "document_versions.tenant_id"],
            name="fk_chunks_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_chunks_document_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("document_version_id", "chunk_no", name="uq_chunks_version_chunk"),
        sa.CheckConstraint("kind IN ('summary', 'table_row', 'text')", name="ck_chunks_kind"),
        sa.CheckConstraint("length(text) <= 4000", name="ck_chunks_text"),
    )
    op.create_index("ix_chunks_tenant_id", "chunks", ["tenant_id"])
    op.create_index("ix_chunks_document", "chunks", ["document_id"])
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(f"GRANT SELECT, INSERT ON chunks TO {_APP_ROLE}")
    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY chunks_tenant ON chunks TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )
    # Certificates are found by the batch they certify (an invoice line's batch number).
    op.execute(
        "CREATE INDEX ix_extractions_batch_no ON extractions "
        "(tenant_id, ((data -> 'batch_no') ->> 'value'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_extractions_batch_no")
    op.drop_table("chunks")
