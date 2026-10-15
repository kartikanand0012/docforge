"""E-invoice QR checks: what each invoice version's signed QR said, as it was read.

`einvoice_checks` holds one row per document version that was scanned: the outcome (verified,
bad_signature, malformed, unknown_key, none or unreadable), the evidence the rules are built
from on every reassessment, the signed QR itself (to check again when keys change), its page,
how many pages were scanned and with which decoder.

Like the extraction it sits beside: its workspace's only (row-level security, a foreign key
that ties its tenant to its version's), append-only, and erased with the account (before the
versions it belongs to). A deleted document's check stays hidden with it, as its extraction
does.

Revision ID: 0039
Revises: 0038
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_STATUSES = ("verified", "bad_signature", "malformed", "unknown_key", "none", "unreadable")
_ERASE_ANCHOR = "DELETE FROM document_versions WHERE tenant_id = t;"
_ERASE_CHECKS = "DELETE FROM einvoice_checks WHERE tenant_id = t;"


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


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    statuses = ", ".join(f"'{status}'" for status in _STATUSES)
    op.create_table(
        "einvoice_checks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("evidence", postgresql.JSONB),
        sa.Column("qr_text", sa.Text),
        sa.Column("page", sa.Integer),
        sa.Column("pages_scanned", sa.Integer, nullable=False),
        sa.Column("decoder", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "tenant_id"],
            ["document_versions.id", "document_versions.tenant_id"],
            name="fk_einvoice_checks_document_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("document_version_id", name="uq_einvoice_checks_version"),
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_einvoice_checks_status"),
        sa.CheckConstraint(
            "qr_text IS NULL OR length(qr_text) <= 4096", name="ck_einvoice_checks_qr_text"
        ),
        sa.CheckConstraint(
            "pages_scanned BETWEEN 0 AND 6 AND (page IS NULL OR page >= 1)",
            name="ck_einvoice_checks_pages",
        ),
        sa.CheckConstraint(
            "decoder IS NULL OR length(decoder) <= 64", name="ck_einvoice_checks_decoder"
        ),
        sa.CheckConstraint(
            "(evidence IS NULL) = (status IN ('none', 'unreadable'))",
            name="ck_einvoice_checks_evidence",
        ),
    )
    op.create_index("ix_einvoice_checks_tenant", "einvoice_checks", ["tenant_id"])
    op.execute(f"REVOKE ALL ON einvoice_checks FROM {_APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON einvoice_checks TO {_APP_ROLE}")
    op.execute("ALTER TABLE einvoice_checks ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY einvoice_checks_tenant ON einvoice_checks TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )
    op.execute(
        "CREATE TRIGGER einvoice_checks_append_only BEFORE UPDATE OR DELETE ON einvoice_checks "
        "FOR EACH ROW EXECUTE FUNCTION docforge_forbid_change()"
    )
    op.execute(
        "CREATE TRIGGER einvoice_checks_no_truncate BEFORE TRUNCATE ON einvoice_checks "
        "FOR EACH STATEMENT EXECUTE FUNCTION docforge_forbid_change()"
    )
    for trigger in ("einvoice_checks_append_only", "einvoice_checks_no_truncate"):
        op.execute(f"ALTER TABLE einvoice_checks ENABLE ALWAYS TRIGGER {trigger}")
    _erase_function(_ERASE_ANCHOR, f"{_ERASE_CHECKS}\n    {_ERASE_ANCHOR}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _erase_function(f"{_ERASE_CHECKS}\n    {_ERASE_ANCHOR}", _ERASE_ANCHOR)
    op.drop_table("einvoice_checks")
