"""Each document names the version its search chunks come from; indexes for operations.

Search shows only a document's newest indexed version. Working that out per chunk, with a
subquery, took 170 of 180 ms of a vector search at 50,000 chunks; read from the document it is
part of the join search already makes.

Written to run on a busy, large database: the column is added alone (a brief lock), filled in
batches, its key added NOT VALID and validated afterwards, and indexes built concurrently, so
reads and writes carry on throughout.

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

_BATCH = 5000

# The newest version with chunks, for documents not yet pointing at one, a batch at a time.
_BACKFILL = sa.text(
    """
    WITH batch AS (
        SELECT id FROM documents
        WHERE indexed_version_id IS NULL AND id > CAST(:after AS uuid)
        ORDER BY id LIMIT :size
    ), newest AS (
        SELECT DISTINCT ON (v.document_id) v.document_id, v.id
        FROM document_versions v JOIN batch b ON b.id = v.document_id
        WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.document_version_id = v.id)
        ORDER BY v.document_id, v.version_no DESC
    ), done AS (
        UPDATE documents d SET indexed_version_id = newest.id
        FROM newest WHERE newest.document_id = d.id
    )
    SELECT max(id::text) FROM batch
    """
)

_INDEXES = (
    # The key below needs a unique index on what it refers to.
    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_document_versions_id_document "
    "ON document_versions (id, document_id)",
    # Checking the key when a version is ever removed.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_indexed_version "
    "ON documents (indexed_version_id) WHERE indexed_version_id IS NOT NULL",
    # The review queue: a tenant's extracted documents, oldest first.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_review_queue "
    "ON documents (tenant_id, created_at, id) WHERE status = 'extracted'",
    # `python -m docforge.ops check`: documents finished, and jobs failed, in the last hour.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_document_versions_finished "
    "ON document_versions (finished_at) WHERE status IN ('succeeded', 'failed')",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_procrastinate_events_failed "
    "ON procrastinate_events (at) WHERE type = 'failed'",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")  # give up rather than queue all traffic
    op.add_column(
        "documents", sa.Column("indexed_version_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    with op.get_context().autocommit_block():
        for statement in _INDEXES:
            op.execute(statement)
        connection = op.get_bind()
        after = "00000000-0000-0000-0000-000000000000"
        while True:
            last = connection.execute(_BACKFILL, {"after": after, "size": _BATCH}).scalar()
            if last is None:
                break
            after = last
        # The version must be one of this document's (and so of its tenant's).
        op.execute(
            "ALTER TABLE documents ADD CONSTRAINT fk_documents_indexed_version_document "
            "FOREIGN KEY (indexed_version_id, id) "
            "REFERENCES document_versions (id, document_id) ON DELETE RESTRICT NOT VALID"
        )
        op.execute(
            "ALTER TABLE documents VALIDATE CONSTRAINT fk_documents_indexed_version_document"
        )


def downgrade() -> None:
    op.drop_constraint("fk_documents_indexed_version_document", "documents", type_="foreignkey")
    op.drop_column("documents", "indexed_version_id")  # its index goes with it
    for name in (
        "ix_procrastinate_events_failed",
        "ix_document_versions_finished",
        "ix_documents_review_queue",
        "uq_document_versions_id_document",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
