"""Review claims: which reviewer has a document open now, so nobody else changes it meanwhile.

A claim is a lease, one row per document, overwritten when it is renewed, taken over or
claimed again after it ran out. An expired row is ignored and left for the next claim to
overwrite: nothing needs to clear them.

Revision ID: 0025
Revises: 0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "review_claims",
        sa.Column("document_id", _UUID, primary_key=True),
        sa.Column(
            "tenant_id", _UUID, sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("reviewer_id", _UUID, nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_review_claims_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id", "tenant_id"],
            ["reviewers.id", "reviewers.tenant_id"],
            name="fk_review_claims_reviewer",
            ondelete="RESTRICT",
        ),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON review_claims TO {_APP_ROLE}")
    op.execute("ALTER TABLE review_claims ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY review_claims_tenant ON review_claims TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("review_claims")
