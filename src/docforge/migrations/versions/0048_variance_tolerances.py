"""V2 P7: the PO variance report's tolerances, one set per workspace.

`workspace_settings` gains four columns: how far a bill's rate (rupees per unit), quantity
and free quantity (units) and line taxable amount (rupees) may differ from its PO and still be
shown as matched, with a note. Rate, quantity and free quantity are exact by default; a
taxable amount within one rupee is matched (rounding). The table keeps its row-level security
and is already erased with the account; no lookup is added, so no index.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("variance_rate_tolerance", sa.Numeric(12, 2), "0", "BETWEEN 0 AND 1000"),
    ("variance_qty_tolerance", sa.Integer(), "0", "BETWEEN 0 AND 100000"),
    ("variance_free_qty_tolerance", sa.Integer(), "0", "BETWEEN 0 AND 100000"),
    ("variance_amount_tolerance", sa.Numeric(12, 2), "1.00", "BETWEEN 0 AND 100000"),
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, kind, default, bounds in _COLUMNS:
        op.add_column(
            "workspace_settings",
            sa.Column(name, kind, nullable=False, server_default=sa.text(default)),
        )
        op.create_check_constraint(
            f"ck_workspace_settings_{name}", "workspace_settings", f"{name} {bounds}"
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, *_ in reversed(_COLUMNS):
        op.drop_constraint(f"ck_workspace_settings_{name}", "workspace_settings", type_="check")
        op.drop_column("workspace_settings", name)
