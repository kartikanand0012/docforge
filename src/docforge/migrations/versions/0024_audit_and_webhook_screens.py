"""The audit log and webhook screens: indexes to filter the log by action, actor and time;
webhooks deleted (kept, never sent to) and their secret's last rotation; each delivery's
last attempt and when the next is due.

The indexes are built CONCURRENTLY: a plain build would block audit appends, and with them
document processing, while it runs.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_log_action "
    "ON audit_log (tenant_id, action, id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_log_actor "
    "ON audit_log (tenant_id, actor, id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_log_occurred "
    "ON audit_log (tenant_id, occurred_at)",
)


def upgrade() -> None:
    # Safe to run again after a failure part way: each step checks what is already there.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table, column in (
        ("webhooks", "deleted_at"),
        ("webhooks", "secret_rotated_at"),
        ("webhook_deliveries", "last_attempt_at"),
        ("webhook_deliveries", "next_attempt_at"),
    ):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} timestamptz")
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'ck_webhooks_deleted_inactive') THEN "
        "ALTER TABLE webhooks ADD CONSTRAINT ck_webhooks_deleted_inactive "
        "CHECK (deleted_at IS NULL OR active = false); END IF; END $$"
    )
    with op.get_context().autocommit_block():
        # A concurrent build that failed leaves an invalid index, which IF NOT EXISTS would
        # keep: drop it first so it is built again.
        for name in ("ix_audit_log_action", "ix_audit_log_actor", "ix_audit_log_occurred"):
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
        for statement in _INDEXES:
            op.execute(statement)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in ("ix_audit_log_occurred", "ix_audit_log_actor", "ix_audit_log_action"):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("webhook_deliveries", "next_attempt_at")
    op.drop_column("webhook_deliveries", "last_attempt_at")
    op.drop_constraint("ck_webhooks_deleted_inactive", "webhooks")
    op.drop_column("webhooks", "secret_rotated_at")
    op.drop_column("webhooks", "deleted_at")
