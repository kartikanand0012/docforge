"""Each document names the version its search chunks come from.

Search shows only a document's newest indexed version. Working that out per chunk, with a
subquery, took 170 of 180 ms of a vector search at 50,000 chunks; read from the document it is
part of the join search already makes.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("indexed_version_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    # The version must be one of the same tenant's.
    op.create_foreign_key(
        "fk_documents_indexed_version_tenant",
        "documents",
        "document_versions",
        ["indexed_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        UPDATE documents d SET indexed_version_id = newest.id
        FROM (
            SELECT DISTINCT ON (v.document_id) v.document_id, v.id
            FROM document_versions v
            WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.document_version_id = v.id)
            ORDER BY v.document_id, v.version_no DESC
        ) newest
        WHERE newest.document_id = d.id
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_documents_indexed_version_tenant", "documents", type_="foreignkey")
    op.drop_column("documents", "indexed_version_id")
