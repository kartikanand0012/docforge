"""What each upload is: its media type, as recognised from its bytes. Every document so far
was a PDF.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # A constant default: Postgres adds the column without rewriting the table.
    op.add_column(
        "documents",
        sa.Column("media_type", sa.Text, nullable=False, server_default="application/pdf"),
    )


def downgrade() -> None:
    op.drop_column("documents", "media_type")
