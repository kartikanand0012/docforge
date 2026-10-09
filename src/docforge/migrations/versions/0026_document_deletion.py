"""Deleted documents: kept, but invisible to the application.

A document is deleted by marking it (`deleted_at`), not by removing it: its versions, audit
trail and signatures still refer to it. The application's role sees only documents that are
not deleted - the policy says so, so a query that forgets to filter still cannot read one -
and cannot mark or unmark one itself. `docforge_delete_document` marks one document of the
tenant in the `docforge.tenant_id` setting, and nothing else.

The content hash identifies a document among the tenant's live documents only, so a file
deleted and uploaded again becomes a new document.

Revision ID: 0026
Revises: 0025
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_LIVE = f"tenant_id = {_TENANT} AND deleted_at IS NULL"
_ALLOW_DOWNGRADE = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"
_DELETE = "docforge_delete_document(document uuid)"
_MARK = (
    "UPDATE documents SET deleted_at = now() "  # noqa: S608 - fixed text, no input in it
    f"WHERE id = document AND tenant_id = {_TENANT} AND deleted_at IS NULL RETURNING true"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.drop_constraint("uq_documents_tenant_sha256", "documents", type_="unique")
    op.create_index(
        "uq_documents_tenant_sha256_live",
        "documents",
        ["tenant_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute("DROP POLICY documents_tenant ON documents")
    op.execute(
        f"CREATE POLICY documents_read ON documents FOR SELECT TO {_APP_ROLE} USING ({_LIVE})"
    )
    op.execute(
        f"CREATE POLICY documents_insert ON documents FOR INSERT TO {_APP_ROLE} "
        f"WITH CHECK ({_LIVE})"
    )
    op.execute(
        f"CREATE POLICY documents_update ON documents FOR UPDATE TO {_APP_ROLE} "
        f"USING ({_LIVE}) WITH CHECK ({_LIVE})"
    )
    op.execute(
        f"CREATE FUNCTION {_DELETE} RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER "
        f"SET search_path = pg_catalog, public, pg_temp AS $$ {_MARK} $$"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_DELETE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_DELETE} TO {_APP_ROLE}")


def downgrade() -> None:
    deleted = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM documents WHERE deleted_at IS NOT NULL)")
    )
    if deleted.scalar() and os.environ.get(_ALLOW_DOWNGRADE) != "1":
        raise RuntimeError(
            "some documents are deleted and this downgrade would show them again; "
            f"set {_ALLOW_DOWNGRADE}=1 to do it anyway"
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP FUNCTION {_DELETE.split('(')[0]}")
    for name in ("documents_read", "documents_insert", "documents_update"):
        op.execute(f"DROP POLICY {name} ON documents")
    op.execute(
        f"CREATE POLICY documents_tenant ON documents TO {_APP_ROLE} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )
    op.drop_index("uq_documents_tenant_sha256_live", "documents")
    op.create_unique_constraint("uq_documents_tenant_sha256", "documents", ["tenant_id", "sha256"])
    op.drop_column("documents", "deleted_at")
