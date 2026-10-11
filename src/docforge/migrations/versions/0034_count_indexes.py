"""Indexes for the counts made on every upload, question and platform screen.

- `ix_audit_log_readings`: today's readings across every free workspace (the safety valve
  of 0029, counted on each upload) and per workspace, without reading the rest of the log.
  Its list of actions is `documents.READINGS`, as is the function's (a test holds them
  together).
- `ix_audit_log_action_time`: one workspace's entries of one action since a time (daily
  limits, the platform's last activity), with the actor for per-person counts.
- `ix_sessions_tenant_created`: a workspace's sign-ins and the newest of them.

Built CONCURRENTLY, outside a transaction, so appends to the audit log and sign-ins go on
while they are built; a build that failed part way is dropped and built again.

Revision ID: 0034
Revises: 0033
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = {
    "ix_audit_log_readings": (
        "ON audit_log (occurred_at, tenant_id) "
        "WHERE action IN ('document.received', 'document.reprocess_requested')"
    ),
    "ix_audit_log_action_time": "ON audit_log (tenant_id, action, occurred_at) INCLUDE (actor)",
    "ix_sessions_tenant_created": "ON sessions (tenant_id, created_at)",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, definition in _INDEXES.items():
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "WHERE c.relname = :name AND NOT i.indisvalid"
                    ),
                    {"name": name},
                )
                .first()
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
