"""An invoice linked to its purchase order by hand, and found again after a correction.

`matches` gains `manual`: true for a link a reviewer made ("Link order"), with its reason in
`data`. An automatic comparison is still stored once per invoice and order version (the
unique pair is now a partial index over those only); a hand link is a row of its own every
time, so linking back to an order linked before makes it the newest again. An index on the
invoice side keeps finding a version's newest comparison quick now that the pair index no
longer covers every row, and an index on corrected order numbers finds an order whose number
a reviewer corrected. Append-only as before: nothing is updated.

Revision ID: 0043
Revises: 0042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "matches",
        sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_constraint("uq_matches_pair", "matches", type_="unique")
    op.create_index(
        "uq_matches_pair",
        "matches",
        ["invoice_version_id", "order_version_id"],
        unique=True,
        postgresql_where=sa.text("NOT manual"),
    )
    op.create_index("ix_matches_invoice_version_id", "matches", ["invoice_version_id"])
    op.create_index(
        "ix_corrections_po_no",
        "corrections",
        ["tenant_id", "new_text"],
        postgresql_where=sa.text("path = 'po_no'"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_corrections_po_no", table_name="corrections")
    op.drop_index("ix_matches_invoice_version_id", table_name="matches")
    op.drop_index("uq_matches_pair", table_name="matches")
    # Links made by hand have no place in the older table: one row per pair.
    op.execute("DELETE FROM matches WHERE manual")
    op.create_unique_constraint(
        "uq_matches_pair", "matches", ["invoice_version_id", "order_version_id"]
    )
    op.drop_column("matches", "manual")
