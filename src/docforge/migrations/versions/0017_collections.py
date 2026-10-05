"""Knowledge bases (collections of an organisation's documents) and the scope of each
conversation: the organisation, one document, or one knowledge base.

The scope is kept on the conversation, not only implied by which id is set: when the
document or knowledge base a conversation was about is deleted, its id becomes NULL, and a
follow-up must then be refused, not widened to the whole organisation.

Conversations from before this migration take their scope from their ids. One about a
document already deleted would read as the organisation's; none can exist, because nothing
deletes documents yet and 0016 was never released before this.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"


def _protect(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant ON {table} TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "collections",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_collections_id_tenant"),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 100", name="ck_collections_name"),
        sa.CheckConstraint("length(description) <= 1000", name="ck_collections_description"),
    )
    # One name per organisation, whatever its case or surrounding spaces.
    op.execute(
        "CREATE UNIQUE INDEX uq_collections_tenant_name "
        "ON collections (tenant_id, lower(btrim(name)))"
    )
    op.create_table(
        "collection_documents",
        sa.Column("collection_id", _UUID, primary_key=True),
        sa.Column("document_id", _UUID, primary_key=True),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["collection_id", "tenant_id"],
            ["collections.id", "collections.tenant_id"],
            name="fk_collection_documents_collection",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_collection_documents_document",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_collection_documents_document", "collection_documents", ["document_id"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON collections TO {_APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON collection_documents TO {_APP_ROLE}")
    _protect("collections")
    _protect("collection_documents")

    op.add_column("conversations", sa.Column("collection_id", _UUID, nullable=True))
    op.create_foreign_key(
        "fk_conversations_collection_tenant",
        "conversations",
        "collections",
        ["collection_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="SET NULL (collection_id)",
    )
    op.add_column(
        "conversations",
        sa.Column("scope", sa.Text, nullable=False, server_default="organisation"),
    )
    op.execute("UPDATE conversations SET scope = 'document' WHERE document_id IS NOT NULL")
    op.create_check_constraint(
        "ck_conversations_scope",
        "conversations",
        "scope IN ('organisation', 'document', 'collection')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_conversations_scope", "conversations")
    op.drop_column("conversations", "scope")
    op.drop_constraint("fk_conversations_collection_tenant", "conversations")
    op.drop_column("conversations", "collection_id")
    op.drop_table("collection_documents")
    op.drop_table("collections")
