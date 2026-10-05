"""How many statements of an answer were dropped: without a quote found in its passage, or
stating a figure none of its quotes holds.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "messages",
        sa.Column("dropped_statements", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("messages", "dropped_statements")
