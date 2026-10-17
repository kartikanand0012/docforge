"""Workspace settings: one row per workspace of the choices it makes for itself.

The first is `near_expiry_months`: stock with less left than this when billed is flagged on
the line (V2 3.4); 0 turns the flag off. No row is the defaults, so nothing is backfilled.
Like every workspace table, its own workspace's only (row-level security) and erased with the
account.

Revision ID: 0041
Revises: 0040
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_ERASE_ANCHOR = "DELETE FROM webhooks WHERE tenant_id = t;"
_ERASE_SETTINGS = "DELETE FROM workspace_settings WHERE tenant_id = t;"


def _erase_function(before: str, after: str) -> None:
    """`docforge_delete_account` as it is, with one statement changed."""
    definition = (
        op.get_bind()
        .execute(sa.text("SELECT pg_get_functiondef('docforge_delete_account'::regproc)"))
        .scalar_one()
    )
    if before not in definition:
        raise RuntimeError(f"docforge_delete_account does not hold {before!r}")
    op.execute(sa.text(definition.replace(before, after, 1)))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "workspace_settings",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("near_expiry_months", sa.Integer, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "near_expiry_months BETWEEN 0 AND 36", name="ck_workspace_settings_near_expiry"
        ),
        sa.CheckConstraint("length(updated_by) <= 200", name="ck_workspace_settings_updated_by"),
    )
    op.execute(f"REVOKE ALL ON workspace_settings FROM {_APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON workspace_settings TO {_APP_ROLE}")
    op.execute("ALTER TABLE workspace_settings ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY workspace_settings_tenant ON workspace_settings TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )
    _erase_function(_ERASE_ANCHOR, f"{_ERASE_ANCHOR}\n    {_ERASE_SETTINGS}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _erase_function(f"{_ERASE_ANCHOR}\n    {_ERASE_SETTINGS}", _ERASE_ANCHOR)
    op.drop_table("workspace_settings")
