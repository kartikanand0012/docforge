"""Integrity hardening: one successor per audit entry, hash formats, stricter triggers, indexes.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX64 = "^[0-9a-f]{64}$"
_TENANT_TABLES = ("document_versions", "parse_outputs", "extractions", "model_runs")
_TRIGGERS = (
    ("extractions", "extractions_append_only"),
    ("extractions", "extractions_no_truncate"),
    ("audit_log", "audit_log_append_only"),
    ("audit_log", "audit_log_no_truncate"),
)


def upgrade() -> None:
    # The chain cannot fork, whoever writes: a tenant has one first entry (prev_hash NULL)
    # and every entry has at most one successor.
    op.execute(
        "ALTER TABLE audit_log ADD CONSTRAINT uq_audit_log_tenant_prev_hash "
        "UNIQUE NULLS NOT DISTINCT (tenant_id, prev_hash)"
    )
    op.create_check_constraint(
        "ck_audit_log_hashes_hex",
        "audit_log",
        f"hash ~ '{_HEX64}' AND (prev_hash IS NULL OR prev_hash ~ '{_HEX64}')",
    )
    op.create_check_constraint("ck_extractions_sha256_hex", "extractions", f"sha256 ~ '{_HEX64}'")

    op.execute(
        "CREATE TRIGGER extractions_no_truncate BEFORE TRUNCATE ON extractions "
        "FOR EACH STATEMENT EXECUTE FUNCTION docforge_forbid_change()"
    )
    # ALWAYS: the triggers also fire when session_replication_role is 'replica', which
    # otherwise skips ordinary triggers.
    for table, trigger in _TRIGGERS:
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}")

    # Tenant filters become constant once row-level security arrives.
    for table in _TENANT_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # A document's trail is read in entry order; include id so that needs no sort.
    op.drop_index("ix_audit_log_target", "audit_log")
    op.create_index(
        "ix_audit_log_target", "audit_log", ["tenant_id", "target_type", "target_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_target", "audit_log")
    op.create_index("ix_audit_log_target", "audit_log", ["tenant_id", "target_type", "target_id"])
    for table in _TENANT_TABLES:
        op.drop_index(f"ix_{table}_tenant_id", table)
    for table, trigger in _TRIGGERS:
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
    op.execute("DROP TRIGGER extractions_no_truncate ON extractions")
    op.drop_constraint("ck_extractions_sha256_hex", "extractions")
    op.drop_constraint("ck_audit_log_hashes_hex", "audit_log")
    op.drop_constraint("uq_audit_log_tenant_prev_hash", "audit_log")
