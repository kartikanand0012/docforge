"""Webhooks and their deliveries (an outbox: written with the change that caused them).

Revision ID: 0009
Revises: 0008

No secret is stored: each webhook's signing secret is derived from a server key and the
webhook's id, so reading this table is not enough to forge a delivery.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALLOW_DOWNGRADE = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"
_UUID = postgresql.UUID(as_uuid=True)
_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_TABLES = ("webhooks", "webhook_deliveries")


def upgrade() -> None:
    op.create_table(
        "webhooks",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("secret_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_webhooks_id_tenant"),
        sa.CheckConstraint("length(url) <= 2000", name="ck_webhooks_url"),
        sa.CheckConstraint("cardinality(events) > 0", name="ck_webhooks_events"),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("webhook_id", _UUID, nullable=False),
        sa.Column("event_id", _UUID, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_status", sa.Integer, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["webhook_id", "tenant_id"],
            ["webhooks.id", "webhooks.tenant_id"],
            name="fk_webhook_deliveries_webhook_tenant",
            ondelete="RESTRICT",
        ),
        # The same event is delivered to a webhook once, however often it is emitted.
        sa.UniqueConstraint("webhook_id", "event_id", name="uq_webhook_deliveries_event"),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')", name="ck_webhook_deliveries_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_webhook_deliveries_attempts"),
    )
    op.create_index("ix_webhooks_tenant_id", "webhooks", ["tenant_id"])
    op.create_index(
        "ix_webhook_deliveries_webhook", "webhook_deliveries", ["webhook_id", "created_at"]
    )
    op.create_index("ix_webhook_deliveries_tenant_id", "webhook_deliveries", ["tenant_id"])
    for table in _TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {_APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} TO {_APP_ROLE} "
            f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
        )


def downgrade() -> None:
    if os.environ.get(_ALLOW_DOWNGRADE) != "1":
        for table in _TABLES:
            query = sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")  # noqa: S608 - fixed names
            if op.get_bind().execute(query).scalar():
                raise RuntimeError(
                    f"{table} has records and this downgrade would discard them; "
                    f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
                )
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")
