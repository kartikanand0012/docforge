"""Why a question went unanswered, and what is known of it, kept with its message: so an
organisation can see what its documents cannot answer, and what to add.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("messages", sa.Column("reason", sa.Text, nullable=True))
    op.add_column(
        "messages",
        sa.Column(
            "reason_detail", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
    )
    # Earlier messages: the reason their status already tells.
    op.execute(
        "UPDATE messages SET reason = CASE status WHEN 'not_found' THEN 'not_in_passages' "
        "WHEN 'unsupported' THEN 'quotes_not_found' WHEN 'error' THEN 'model_error' END "
        "WHERE status IN ('not_found', 'unsupported', 'error')"
    )
    # The organisation's unanswered questions, newest first.
    op.create_index(
        "ix_messages_unanswered",
        "messages",
        ["tenant_id", "created_at"],
        postgresql_where=sa.text("reason IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_unanswered", "messages")
    op.drop_column("messages", "reason_detail")
    op.drop_column("messages", "reason")
