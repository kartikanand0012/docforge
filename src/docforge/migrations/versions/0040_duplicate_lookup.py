"""Duplicate bills: the indexes that find an invoice received before.

Expression indexes only - no table and no data change. An invoice is a duplicate of an earlier
one with the same IRN, or the same seller GSTIN and invoice number (compared in lower case and
without whitespace), the number as read or as a reviewer corrected it. Each expression is
spelled exactly as in `docforge.db.models`, or the index would not be used.

Revision ID: 0040
Revises: 0039
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# As `docforge.db.models._SPACES`: whitespace, and the no-break and zero-width spaces a locale's
# [[:space:]] leaves out.
_SPACES = "'[[:space:]\u00a0\u2007\u202f\u200b\ufeff]'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "CREATE INDEX ix_extractions_invoice_no_key ON extractions (tenant_id, "
        f"lower(regexp_replace(((data -> 'invoice_no') ->> 'value'), {_SPACES}, '', 'g')))"
    )
    op.execute(
        "CREATE INDEX ix_corrections_invoice_no_key ON corrections (tenant_id, "
        f"lower(regexp_replace(new_text, {_SPACES}, '', 'g'))) WHERE path = 'invoice_no'"
    )
    op.execute(
        "CREATE INDEX ix_einvoice_checks_irn ON einvoice_checks "
        "(tenant_id, ((evidence -> 'invoice') ->> 'irn')) WHERE status = 'verified'"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX ix_einvoice_checks_irn")
    op.execute("DROP INDEX ix_corrections_invoice_no_key")
    op.execute("DROP INDEX ix_extractions_invoice_no_key")
