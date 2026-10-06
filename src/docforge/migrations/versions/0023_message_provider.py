"""Which provider answered each chat question: with the model and tokens already kept, the
cost of an answer by provider, for usage metering.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("messages", sa.Column("provider", sa.Text, nullable=True))
    # Every answer before this one was Gemini's: it was the only provider.
    op.execute("UPDATE messages SET provider = 'gemini' WHERE model IS NOT NULL")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("messages", "provider")
