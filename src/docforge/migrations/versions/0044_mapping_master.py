"""The mapping master and export runs (V2 P1).

- `supplier_mappings`: how the workspace's books (Tally, Marg) name a supplier, one per
  seller GSTIN.
- `item_mappings`: how they name a product, one per printed name and pack.
- `mapping_masters`: names the books already use, imported to seed suggestions.
- `export_runs`: each export, immutable - its task list, or its stored file's name and hash.

Row-level security like every workspace table; erased with the account. The lists are read
in name order and searched within one workspace, so each has a (tenant, sort key, id) index in
the "C" collation the keyset cursor compares by.

Revision ID: 0044
Revises: 0043
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_ERASE_ANCHOR = "DELETE FROM h1_products WHERE tenant_id = t;"
_ERASE_NEW = (
    "DELETE FROM supplier_mappings WHERE tenant_id = t;\n"
    "    DELETE FROM item_mappings WHERE tenant_id = t;\n"
    "    DELETE FROM mapping_masters WHERE tenant_id = t;\n"
    "    DELETE FROM export_runs WHERE tenant_id = t;"
)
_GRANTS = {
    "supplier_mappings": "SELECT, INSERT, UPDATE, DELETE",
    "item_mappings": "SELECT, INSERT, UPDATE, DELETE",
    "mapping_masters": "SELECT, INSERT, DELETE",
    "export_runs": "SELECT, INSERT",  # a run is never changed
}


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


def _common() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
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
    ]


def _who() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _edited() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("updated_by", sa.Text),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    ]


def _names(table: str, *columns: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(f"length({c}) BETWEEN 1 AND 200", name=f"ck_{table}_{c}")
        for c in columns
    ]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "supplier_mappings",
        *_common(),
        sa.Column("seller_gstin", sa.Text, nullable=False),
        sa.Column("printed_name", sa.Text, nullable=False),
        sa.Column("sort_key", sa.Text(collation="C"), nullable=False),
        sa.Column("tally_ledger", sa.Text),
        sa.Column("marg_party", sa.Text),
        sa.Column("purchase_ledger", sa.Text),
        sa.Column("tax_treatment", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        *_who(),
        *_edited(),
        sa.UniqueConstraint("tenant_id", "seller_gstin", name="uq_supplier_mappings_gstin"),
        sa.CheckConstraint("seller_gstin ~ '^[0-9A-Z]{15}$'", name="ck_supplier_mappings_gstin"),
        sa.CheckConstraint(
            "length(printed_name) BETWEEN 1 AND 300", name="ck_supplier_mappings_printed_name"
        ),
        sa.CheckConstraint(
            "tally_ledger IS NOT NULL OR marg_party IS NOT NULL",
            name="ck_supplier_mappings_booked",
        ),
        sa.CheckConstraint(
            "length(tax_treatment) BETWEEN 1 AND 100", name="ck_supplier_mappings_tax_treatment"
        ),
        sa.CheckConstraint("source IN ('typed', 'suggestion')", name="ck_supplier_mappings_source"),
        *_names("supplier_mappings", "tally_ledger", "marg_party", "purchase_ledger"),
    )
    op.create_index(
        "ix_supplier_mappings_sort", "supplier_mappings", ["tenant_id", "sort_key", "id"]
    )
    op.create_table(
        "item_mappings",
        *_common(),
        sa.Column("item_key", sa.Text, nullable=False),
        sa.Column("product_name", sa.Text, nullable=False),
        sa.Column("pack", sa.Text),
        sa.Column("printed_hsn", sa.Text),
        sa.Column("sort_key", sa.Text(collation="C"), nullable=False),
        sa.Column("tally_item", sa.Text),
        sa.Column("marg_item", sa.Text),
        sa.Column("preferred_hsn", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        *_who(),
        *_edited(),
        sa.UniqueConstraint("tenant_id", "item_key", name="uq_item_mappings_key"),
        sa.CheckConstraint("length(item_key) BETWEEN 2 AND 420", name="ck_item_mappings_key"),
        sa.CheckConstraint(
            "length(product_name) BETWEEN 1 AND 300", name="ck_item_mappings_product_name"
        ),
        sa.CheckConstraint("length(pack) BETWEEN 1 AND 100", name="ck_item_mappings_pack"),
        sa.CheckConstraint(
            "length(printed_hsn) BETWEEN 1 AND 20", name="ck_item_mappings_printed_hsn"
        ),
        sa.CheckConstraint(
            "preferred_hsn ~ '^[0-9]{4}([0-9]{2}){0,2}$'", name="ck_item_mappings_preferred_hsn"
        ),
        sa.CheckConstraint(
            "tally_item IS NOT NULL OR marg_item IS NOT NULL", name="ck_item_mappings_booked"
        ),
        sa.CheckConstraint("source IN ('typed', 'suggestion')", name="ck_item_mappings_source"),
        *_names("item_mappings", "tally_item", "marg_item"),
    )
    op.create_index("ix_item_mappings_sort", "item_mappings", ["tenant_id", "sort_key", "id"])
    op.create_table(
        "mapping_masters",
        *_common(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_key", sa.Text, nullable=False),
        sa.Column("gstin", sa.Text),
        *_who(),
        sa.UniqueConstraint("tenant_id", "kind", "name_key", name="uq_mapping_masters_name"),
        sa.CheckConstraint("kind IN ('supplier', 'item')", name="ck_mapping_masters_kind"),
        sa.CheckConstraint("gstin ~ '^[0-9A-Z]{15}$'", name="ck_mapping_masters_gstin"),
        *_names("mapping_masters", "name"),
        # Case-folding may lengthen a name ("ß" is "ss").
        sa.CheckConstraint("length(name_key) BETWEEN 1 AND 400", name="ck_mapping_masters_key"),
    )
    op.create_table(
        "export_runs",
        *_common(),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("range_start", sa.Date, nullable=False),
        sa.Column("range_end", sa.Date, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("invoices", sa.Integer, nullable=False),
        sa.Column("lines", sa.Integer, nullable=False),
        sa.Column("tasks", postgresql.JSONB),
        sa.Column("file_key", sa.Text),
        sa.Column("filename", sa.Text),
        sa.Column("sha256", postgresql.CHAR(64)),
        sa.Column("size_bytes", sa.Integer),
        *_who(),
        sa.CheckConstraint("target IN ('tally', 'marg')", name="ck_export_runs_target"),
        sa.CheckConstraint("status IN ('needs_mapping', 'ready')", name="ck_export_runs_status"),
        sa.CheckConstraint("range_end >= range_start", name="ck_export_runs_range"),
        sa.CheckConstraint(
            "(status = 'ready') = (file_key IS NOT NULL AND sha256 IS NOT NULL "
            "AND filename IS NOT NULL AND size_bytes IS NOT NULL)",
            name="ck_export_runs_file",
        ),
        sa.CheckConstraint("(status = 'ready') = (tasks IS NULL)", name="ck_export_runs_tasks"),
    )
    op.create_index(
        "ix_export_runs_created",
        "export_runs",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    for table, grants in _GRANTS.items():
        op.execute(f"REVOKE ALL ON {table} FROM {_APP_ROLE}")
        op.execute(f"GRANT {grants} ON {table} TO {_APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} TO {_APP_ROLE} "
            f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
        )
    _erase_function(_ERASE_ANCHOR, f"{_ERASE_ANCHOR}\n    {_ERASE_NEW}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _erase_function(f"{_ERASE_ANCHOR}\n    {_ERASE_NEW}", _ERASE_ANCHOR)
    for table in reversed(list(_GRANTS)):
        op.drop_table(table)
