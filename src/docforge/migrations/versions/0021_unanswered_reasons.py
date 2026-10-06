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
    with op.get_context().autocommit_block():
        # Earlier messages: the reason their status tells. Why an older answer was withheld
        # was not recorded, so it is said to be unknown rather than guessed.
        op.execute(
            "UPDATE messages SET reason = CASE status WHEN 'not_found' THEN 'not_in_passages' "
            "WHEN 'unsupported' THEN 'not_recorded' WHEN 'error' THEN 'model_error' END "
            "WHERE status IN ('not_found', 'unsupported', 'error') AND reason IS NULL"
        )
        # The organisation's unanswered questions, newest first; built without blocking writes.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_messages_unanswered "
            "ON messages (tenant_id, created_at) WHERE reason IS NOT NULL"
        )


def downgrade() -> None:
    op.drop_index("ix_messages_unanswered", "messages")
    op.drop_column("messages", "reason_detail")
    op.drop_column("messages", "reason")
