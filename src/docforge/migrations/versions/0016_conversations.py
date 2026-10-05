"""Conversations and messages for chat with documents, per tenant under row-level security.

Revision ID: 0016
Revises: 0015
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"


def _tenant_column() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _protect(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant ON {table} TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("document_id", _UUID, nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_conversations_document_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_conversations_id_tenant"),
        sa.CheckConstraint("length(title) <= 200", name="ck_conversations_title"),
    )
    op.create_index("ix_conversations_owner", "conversations", ["tenant_id", "owner", "created_at"])
    op.create_table(
        "messages",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("citations", postgresql.JSONB, nullable=False),
        sa.Column("dropped_citations", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["conversations.id", "conversations.tenant_id"],
            name="fk_messages_conversation_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('supported', 'partly_supported', 'unsupported', 'not_found')",
            name="ck_messages_status",
        ),
        sa.CheckConstraint("length(question) <= 2000", name="ck_messages_question"),
    )
    op.create_index("ix_messages_conversation", "messages", ["conversation_id", "created_at"])
    # The daily question limit counts an organisation's messages since midnight.
    op.create_index("ix_messages_tenant_created", "messages", ["tenant_id", "created_at"])
    # Conversations can be deleted with their messages (a person's questions are theirs).
    op.execute(f"GRANT SELECT, INSERT, DELETE ON conversations, messages TO {_APP_ROLE}")
    _protect("conversations")
    _protect("messages")


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
