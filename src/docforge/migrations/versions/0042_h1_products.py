"""Schedule H1 products a workspace has marked: bill lines that name a brand, not a molecule.

A reviewer marks a line's product as H1 once; every later line whose product name has the same
key (lower case, letters, digits and decimal points) is H1 in that workspace (V2 3.6). One row
per product and workspace; a mark is removed by deleting its row. Row-level security like
every workspace table; erased with the account.

Revision ID: 0042
Revises: 0041
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_ERASE_ANCHOR = "DELETE FROM workspace_settings WHERE tenant_id = t;"
_ERASE_PRODUCTS = "DELETE FROM h1_products WHERE tenant_id = t;"


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
        "h1_products",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("product_key", sa.Text, nullable=False),
        sa.Column("product_name", sa.Text, nullable=False),
        sa.Column("marked_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "product_key", name="uq_h1_products_tenant_key"),
        sa.CheckConstraint(
            "length(product_key) BETWEEN 1 AND 200", name="ck_h1_products_product_key"
        ),
        sa.CheckConstraint(
            "length(product_name) BETWEEN 1 AND 300", name="ck_h1_products_product_name"
        ),
        sa.CheckConstraint("length(marked_by) <= 200", name="ck_h1_products_marked_by"),
    )
    op.execute(f"REVOKE ALL ON h1_products FROM {_APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON h1_products TO {_APP_ROLE}")
    op.execute("ALTER TABLE h1_products ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY h1_products_tenant ON h1_products TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )
    _erase_function(_ERASE_ANCHOR, f"{_ERASE_ANCHOR}\n    {_ERASE_PRODUCTS}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _erase_function(f"{_ERASE_ANCHOR}\n    {_ERASE_PRODUCTS}", _ERASE_ANCHOR)
    op.drop_table("h1_products")
