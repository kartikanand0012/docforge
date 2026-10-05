"""Caps that hold across every API process: requests counted per minute, and leases for
what is held at once (questions in flight, open streams). Keyed by organisation and caller;
no document content. A lease expires, so one left by a process that died frees itself.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"


def upgrade() -> None:
    op.create_table(
        "rate_windows",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("count", sa.Integer, nullable=False),
    )
    op.create_index("ix_rate_windows_start", "rate_windows", ["window_start"])
    op.create_table(
        "leases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_leases_key", "leases", ["key", "expires_at"])
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON rate_windows, leases TO {_APP_ROLE}")


def downgrade() -> None:
    op.drop_table("leases")
    op.drop_table("rate_windows")
