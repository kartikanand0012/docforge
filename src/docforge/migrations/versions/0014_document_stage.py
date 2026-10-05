"""Where each document is: stored, parsing, extracting, checking, indexing, processed, ready,
retrying or failed. The audit trail keeps the history; this column is the cheap current read.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "documents",
        sa.Column("stage", sa.Text, nullable=False, server_default="stored"),
    )
    # Documents from before: their stage from their status and whether search has them.
    op.execute(
        """
        UPDATE documents SET stage = CASE
            WHEN status = 'failed' THEN 'failed'
            WHEN status = 'processing' THEN 'parsing'
            WHEN status = 'extracted' AND indexed_version_id IS NOT NULL THEN 'ready'
            WHEN status = 'extracted' THEN 'processed'
            ELSE 'stored'
        END
        """
    )


def downgrade() -> None:
    op.drop_column("documents", "stage")
