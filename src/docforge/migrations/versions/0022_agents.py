"""AI agents: a read-only role for API keys, who made a key, and a record of every call an
agent makes through the MCP server (which tool, on what, how it went) - never the question,
the query or any document text.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_api_keys_role", "api_keys")
    op.create_check_constraint(
        "ck_api_keys_role", "api_keys", "role IN ('integrator', 'reviewer', 'admin', 'reader')"
    )
    op.add_column("api_keys", sa.Column("created_by", sa.Text, nullable=True))
    op.create_table(
        "agent_calls",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "key_id", _UUID, sa.ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("document_id", _UUID, nullable=True),
        sa.Column("collection_id", _UUID, nullable=True),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("results", sa.Integer, nullable=False, server_default="0"),
        sa.Column("message_id", _UUID, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "outcome IN ('ok', 'not_found', 'limited', 'invalid', 'error')",
            name="ck_agent_calls_outcome",
        ),
        sa.CheckConstraint(
            "scope IN ('organisation', 'document', 'collection')", name="ck_agent_calls_scope"
        ),
    )
    op.create_index("ix_agent_calls_tenant_created", "agent_calls", ["tenant_id", "created_at"])
    op.create_index("ix_agent_calls_key", "agent_calls", ["key_id"])
    # Appended and read; never changed.
    op.execute(f"GRANT SELECT, INSERT ON agent_calls TO {_APP_ROLE}")
    op.execute("ALTER TABLE agent_calls ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY agent_calls_tenant ON agent_calls TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("agent_calls")
    op.drop_column("api_keys", "created_by")
    op.execute("DELETE FROM api_keys WHERE role = 'reader'")
    op.drop_constraint("ck_api_keys_role", "api_keys")
    op.create_check_constraint(
        "ck_api_keys_role", "api_keys", "role IN ('integrator', 'reviewer', 'admin')"
    )
