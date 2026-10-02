"""Tenant consistency between records, match sides, and lookup indexes.

Revision ID: 0005
Revises: 0004

Every record carries its own `tenant_id` next to the version it belongs to. Until now nothing
made the two agree, so a row could carry one tenant and point at another's version. Composite
foreign keys now tie them together, which row-level security (C6) relies on.

The constraints are validated against existing rows while the table is locked. That is fine at
this size; on a large table they would be added NOT VALID and validated separately, and the
indexes built CONCURRENTLY.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) pairs that reference a document version.
_VERSION_REFERENCES = (
    ("parse_outputs", "document_version_id"),
    ("extractions", "document_version_id"),
    ("model_runs", "document_version_id"),
    ("assessments", "document_version_id"),
    ("matches", "invoice_version_id"),
    ("matches", "order_version_id"),
)

_MATCH_SIDES = """
CREATE FUNCTION docforge_match_sides() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    invoice_type text;
    order_type text;
BEGIN
    SELECT d.doc_type INTO invoice_type FROM document_versions v
        JOIN documents d ON d.id = v.document_id WHERE v.id = NEW.invoice_version_id;
    SELECT d.doc_type INTO order_type FROM document_versions v
        JOIN documents d ON d.id = v.document_id WHERE v.id = NEW.order_version_id;
    IF invoice_type IS DISTINCT FROM 'invoice' OR order_type IS DISTINCT FROM 'purchase_order' THEN
        RAISE EXCEPTION 'a match pairs an invoice with a purchase order, not % with %',
            invoice_type, order_type USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""


def _name(table: str, column: str) -> str:
    return f"fk_{table}_{column.removesuffix('_id')}_tenant"


def upgrade() -> None:
    op.create_unique_constraint("uq_documents_id_tenant", "documents", ["id", "tenant_id"])
    op.create_unique_constraint(
        "uq_document_versions_id_tenant", "document_versions", ["id", "tenant_id"]
    )
    op.create_foreign_key(
        "fk_document_versions_document_tenant",
        "document_versions",
        "documents",
        ["document_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    for table, column in _VERSION_REFERENCES:
        op.create_foreign_key(
            _name(table, column),
            table,
            "document_versions",
            [column, "tenant_id"],
            ["id", "tenant_id"],
            ondelete="RESTRICT",
        )
    op.execute(_MATCH_SIDES)
    op.execute(
        "CREATE TRIGGER matches_sides BEFORE INSERT ON matches "
        "FOR EACH ROW EXECUTE FUNCTION docforge_match_sides()"
    )
    op.execute("ALTER TABLE matches ENABLE ALWAYS TRIGGER matches_sides")
    # The counterpart lookup: same tenant, other type, same order number.
    op.execute(
        "CREATE INDEX ix_extractions_po_no ON extractions "
        "(tenant_id, ((data -> 'po_no') ->> 'value'))"
    )
    op.create_index("ix_documents_tenant_type", "documents", ["tenant_id", "doc_type"])


def downgrade() -> None:
    op.drop_index("ix_documents_tenant_type", "documents")
    op.execute("DROP INDEX ix_extractions_po_no")
    op.execute("DROP TRIGGER matches_sides ON matches")
    op.execute("DROP FUNCTION docforge_match_sides()")
    for table, column in reversed(_VERSION_REFERENCES):
        op.drop_constraint(_name(table, column), table, type_="foreignkey")
    op.drop_constraint(
        "fk_document_versions_document_tenant", "document_versions", type_="foreignkey"
    )
    op.drop_constraint("uq_document_versions_id_tenant", "document_versions", type_="unique")
    op.drop_constraint("uq_documents_id_tenant", "documents", type_="unique")
