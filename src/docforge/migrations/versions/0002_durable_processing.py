"""Durable processing: versions, parse output, immutable extractions, model runs, audit log, queue.

Revision ID: 0002
Revises: 0001
"""

import os
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_ALLOW_DOWNGRADE = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"
_QUEUE_SCHEMA = Path(__file__).resolve().parents[1] / "sql" / "procrastinate_3_10_0.sql"
_UUID = postgresql.UUID(as_uuid=True)

# One function serves every append-only table. Class 23 makes it an integrity error.
_FORBID_CHANGE = """
CREATE FUNCTION docforge_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

# Dropping the queue means dropping every object whose name starts with procrastinate_.
_DROP_QUEUE = """
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT tablename FROM pg_tables
        WHERE schemaname = current_schema() AND tablename LIKE 'procrastinate\\_%' LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', item.tablename);
    END LOOP;
    FOR item IN SELECT oid::regprocedure AS signature FROM pg_proc
        WHERE pronamespace = current_schema()::regnamespace
        AND proname LIKE 'procrastinate\\_%' LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', item.signature);
    END LOOP;
    FOR item IN SELECT typname FROM pg_type
        WHERE typnamespace = current_schema()::regnamespace AND typname LIKE 'procrastinate\\_%'
        AND typtype IN ('e', 'c') LOOP
        EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', item.typname);
    END LOOP;
END;
$$;
"""


def _id() -> sa.Column[uuid.UUID]:
    return sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )


def _version() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "document_version_id",
        _UUID,
        sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _append_only(table: str, *, truncate: bool = False) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION docforge_forbid_change()"
    )
    if truncate:
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION docforge_forbid_change()"
        )


def upgrade() -> None:
    # The queue lives in the same database so a job is enqueued in the same transaction
    # as the rows it works on. A later Procrastinate upgrade needs its own migration here.
    # Run on the driver connection without parameters: the script contains literal % signs.
    driver_connection = op.get_bind().connection.driver_connection
    assert driver_connection is not None  # noqa: S101 - narrows the type; Alembic is online
    driver_connection.execute(_QUEUE_SCHEMA.read_text(encoding="utf-8"))
    op.execute(_FORBID_CHANGE)

    # Single tenant until C6; every row already carries a tenant.
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name) VALUES (CAST(:id AS uuid), 'default') "
            "ON CONFLICT DO NOTHING"
        ).bindparams(id=DEFAULT_TENANT_ID)
    )

    # Rows created before this migration are filled in before the columns become required.
    # Until now every document was an invoice; sizes were not recorded, hence 0.
    op.execute("UPDATE documents SET doc_type = 'invoice' WHERE doc_type IS NULL")
    op.alter_column("documents", "doc_type", nullable=False)
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger))
    op.execute("UPDATE documents SET size_bytes = 0")
    op.alter_column("documents", "size_bytes", nullable=False)
    op.add_column("documents", sa.Column("page_count", sa.Integer))
    op.create_check_constraint(
        "ck_documents_status",
        "documents",
        "status IN ('received', 'processing', 'extracted', 'failed')",
    )

    op.add_column(
        "document_versions",
        sa.Column("tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT")),
    )
    op.execute(
        "UPDATE document_versions v SET tenant_id = d.tenant_id FROM documents d "
        "WHERE d.id = v.document_id"
    )
    op.alter_column("document_versions", "tenant_id", nullable=False)
    op.add_column(
        "document_versions", sa.Column("status", sa.Text, nullable=False, server_default="queued")
    )
    op.add_column(
        "document_versions", sa.Column("attempts", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column("document_versions", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("document_versions", sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.add_column("document_versions", sa.Column("error", sa.Text))
    # A version that existed before the queue did has no job; it must not look queued.
    op.execute(
        "UPDATE document_versions SET status = 'failed', "
        "error = 'Created before processing was tracked.'"
    )
    op.create_check_constraint(
        "ck_document_versions_status",
        "document_versions",
        "status IN ('queued', 'running', 'succeeded', 'failed')",
    )

    op.create_table(
        "parse_outputs",
        _id(),
        _tenant(),
        _version(),
        sa.Column("data", postgresql.JSONB, nullable=False),
        _created_at(),
        sa.UniqueConstraint("document_version_id", name="uq_parse_outputs_version"),
    )

    op.create_table(
        "extractions",
        _id(),
        _tenant(),
        _version(),
        sa.Column("schema_version", sa.Text, nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),
        # Hash of the canonical JSON of `data`: what an audit entry or signature refers to.
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        _created_at(),
        sa.UniqueConstraint("document_version_id", name="uq_extractions_version"),
    )
    _append_only("extractions")

    op.create_table(
        "model_runs",
        _id(),
        _tenant(),
        _version(),
        sa.Column("call_no", sa.Integer, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("thinking_tokens", sa.Integer),
        sa.Column("latency_ms", sa.Float, nullable=False),
        _created_at(),
        sa.UniqueConstraint("document_version_id", "call_no", name="uq_model_runs_version_call"),
    )

    op.create_table(
        "audit_log",
        # A sequence, not a UUID: the chain is verified in insertion order.
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        _tenant(),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target_type", sa.Text, nullable=False),
        sa.Column("target_id", sa.Text, nullable=False),
        sa.Column("details", postgresql.JSONB, nullable=False),
        sa.Column("prev_hash", sa.CHAR(64)),  # null only for a tenant's first entry
        sa.Column("hash", sa.CHAR(64), nullable=False, unique=True),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id", "id"])
    op.create_index("ix_audit_log_target", "audit_log", ["tenant_id", "target_type", "target_id"])
    _append_only("audit_log", truncate=True)


def downgrade() -> None:
    # Dropping these tables discards records that are meant to be permanent.
    if os.environ.get(_ALLOW_DOWNGRADE) != "1":
        for table in ("audit_log", "extractions"):
            query = sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")  # noqa: S608 - fixed names
            if op.get_bind().execute(query).scalar():
                raise RuntimeError(
                    f"{table} has records and this downgrade would discard them; "
                    f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
                )
    op.drop_table("audit_log")
    op.drop_table("model_runs")
    op.drop_table("extractions")
    op.drop_table("parse_outputs")
    op.execute("DROP FUNCTION docforge_forbid_change()")

    op.drop_constraint("ck_document_versions_status", "document_versions")
    for column in ("error", "finished_at", "started_at", "attempts", "status", "tenant_id"):
        op.drop_column("document_versions", column)

    op.drop_constraint("ck_documents_status", "documents")
    op.drop_column("documents", "page_count")
    op.drop_column("documents", "size_bytes")
    op.alter_column("documents", "doc_type", nullable=True)

    op.execute(
        sa.text(
            "DELETE FROM tenants t WHERE t.id = CAST(:id AS uuid) "
            "AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.tenant_id = t.id)"
        ).bindparams(id=DEFAULT_TENANT_ID)
    )
    op.execute(_DROP_QUEUE)
