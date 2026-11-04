"""V2 P6: bulk upload, and one PDF holding several invoices split into one document each.

- `documents.parent_document_id`, `documents.parent_pages`: an invoice cut from a PDF that
  held several - that upload, and which of its pages (1-based). Both or neither.
- `documents.status` gains `split` (the file was divided into its invoices; it is no bill
  itself) and `split_review` (a person is to confirm how its pages group, before anything is
  made); `document_versions.status` gains `split` (the reading ended in a division).
- `document_splits`: per divided file, the proposal (groups, "not an invoice" pages, pages in
  doubt and why), and once made the groups with their documents, and who confirmed them.
- `upload_batches`, `upload_batch_items`: files dropped together or one ZIP, and what became
  of each file when it arrived.

Row-level security on each new table; erased with the account. Reversible.

Revision ID: 0047
Revises: 0046
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_SPLITS_ANCHOR = "DELETE FROM document_versions WHERE tenant_id = t;"
_SPLITS_NEW = "DELETE FROM document_splits WHERE tenant_id = t;"
_BATCHES_ANCHOR = "DELETE FROM intake_addresses WHERE tenant_id = t;"
_BATCHES_NEW = (
    "DELETE FROM upload_batch_items WHERE tenant_id = t;\n"
    "    DELETE FROM upload_batches WHERE tenant_id = t;"
)
_GRANTS = {
    "document_splits": "SELECT, INSERT, UPDATE",
    "upload_batches": "SELECT, INSERT",
    "upload_batch_items": "SELECT, INSERT, UPDATE",
}
_DOCUMENT_STATUS = ("received", "processing", "extracted", "failed")
_VERSION_STATUS = ("queued", "running", "succeeded", "failed")


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


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


def _key() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _created() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _status_check(table: str, name: str, values: Sequence[str]) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"status IN ({_listed(values)})")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "documents",
        sa.Column(
            "parent_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
        ),
    )
    op.add_column("documents", sa.Column("parent_pages", postgresql.ARRAY(sa.Integer)))
    op.create_check_constraint(
        "ck_documents_parent",
        "documents",
        "(parent_document_id IS NULL) = (parent_pages IS NULL) AND "
        "(parent_pages IS NULL OR cardinality(parent_pages) >= 1)",
    )
    op.create_index(
        "ix_documents_parent",
        "documents",
        ["tenant_id", "parent_document_id"],
        postgresql_where=sa.text("parent_document_id IS NOT NULL"),
    )
    _status_check("documents", "ck_documents_status", (*_DOCUMENT_STATUS, "split", "split_review"))
    _status_check("document_versions", "ck_document_versions_status", (*_VERSION_STATUS, "split"))

    op.create_table(
        "document_splits",
        _key(),
        _tenant(),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("automatic", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("page_count", sa.Integer, nullable=False),
        sa.Column("proposal", postgresql.JSONB, nullable=False),
        sa.Column("groups", postgresql.JSONB),
        sa.Column("non_invoice", postgresql.ARRAY(sa.Integer)),
        sa.Column("decided_by", sa.Text),
        sa.Column("decided_person", sa.Text),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        _created(),
        sa.CheckConstraint("status IN ('review', 'split')", name="ck_document_splits_status"),
        sa.CheckConstraint("page_count >= 2", name="ck_document_splits_pages"),
        sa.CheckConstraint(
            "(status = 'split') = (groups IS NOT NULL AND non_invoice IS NOT NULL)",
            name="ck_document_splits_made",
        ),
        sa.UniqueConstraint("document_id", name="uq_document_splits_document"),
    )
    op.create_index(
        "ix_document_splits_status",
        "document_splits",
        ["tenant_id", "status", sa.text("created_at DESC")],
    )

    op.create_table(
        "upload_batches",
        _key(),
        _tenant(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("doc_type", sa.Text, nullable=False),
        sa.Column("expected", sa.Integer, nullable=False),
        sa.Column("name", sa.Text),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("person", sa.Text, nullable=False),
        _created(),
        sa.CheckConstraint("kind IN ('files', 'zip')", name="ck_upload_batches_kind"),
        sa.CheckConstraint("expected BETWEEN 1 AND 200", name="ck_upload_batches_expected"),
    )
    op.create_index(
        "ix_upload_batches_created",
        "upload_batches",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_table(
        "upload_batch_items",
        _key(),
        _tenant(),
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("upload_batches.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        # No foreign key: a batch's record outlives a deleted document.
        sa.Column("document_id", postgresql.UUID(as_uuid=True)),
        _created(),
        sa.CheckConstraint(
            "outcome IN ('received', 'already_received', 'refused')",
            name="ck_upload_batch_items_outcome",
        ),
        sa.CheckConstraint(
            "(outcome = 'refused') = (document_id IS NULL)", name="ck_upload_batch_items_document"
        ),
        sa.UniqueConstraint("batch_id", "position", name="uq_upload_batch_items_position"),
    )
    op.create_index("ix_upload_batch_items_batch", "upload_batch_items", ["tenant_id", "batch_id"])
    op.create_index(
        "ix_upload_batch_items_document",
        "upload_batch_items",
        ["tenant_id", "document_id"],
        postgresql_where=sa.text("document_id IS NOT NULL"),
    )

    for table, grants in _GRANTS.items():
        op.execute(f"REVOKE ALL ON {table} FROM {_APP_ROLE}")
        op.execute(f"GRANT {grants} ON {table} TO {_APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} TO {_APP_ROLE} "
            f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
        )
    _erase_function(_SPLITS_ANCHOR, f"{_SPLITS_NEW}\n    {_SPLITS_ANCHOR}")
    _erase_function(_BATCHES_ANCHOR, f"{_BATCHES_ANCHOR}\n    {_BATCHES_NEW}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _erase_function(f"{_BATCHES_ANCHOR}\n    {_BATCHES_NEW}", _BATCHES_ANCHOR)
    _erase_function(f"{_SPLITS_NEW}\n    {_SPLITS_ANCHOR}", _SPLITS_ANCHOR)
    for table in ("upload_batch_items", "upload_batches", "document_splits"):
        op.drop_table(table)
    # Rows in the statuses going away: a divided file is kept as a failed reading.
    op.execute(
        "UPDATE document_versions SET status = 'failed', error = 'Split into invoices.' "
        "WHERE status = 'split'"
    )
    op.execute(
        "UPDATE documents SET status = 'failed', stage = 'failed' "
        "WHERE status IN ('split', 'split_review')"
    )
    _status_check("document_versions", "ck_document_versions_status", _VERSION_STATUS)
    _status_check("documents", "ck_documents_status", _DOCUMENT_STATUS)
    op.drop_index("ix_documents_parent", table_name="documents")
    op.drop_constraint("ck_documents_parent", "documents", type_="check")
    op.drop_column("documents", "parent_pages")
    op.drop_column("documents", "parent_document_id")
