"""A reviewer account can be shared: the public demo's, whose PIN is on its sign-in page.

A shared account is never locked by wrong PINs: the PIN is public, so a lock would protect
nothing and would let any visitor shut every other visitor out.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # A constant default: no table rewrite on PostgreSQL 11 and later.
    op.add_column(
        "reviewers",
        sa.Column("shared", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("reviewers", "shared")
