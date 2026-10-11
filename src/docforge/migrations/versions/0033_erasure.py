"""Erasure: a person deletes their account and everything in its workspace; a deleted
document's stored files are removed.

- `docforge_delete_account()` deletes the personal workspace in the `docforge.tenant_id`
  setting - every row of it, in every table, then the workspace itself - and records that
  its stored files are to be erased. It refuses any other workspace: an organisation's, or
  none. The application calls it only after the person has given their password again.
- The append-only tables (audit log, extractions, reviews...) and reviewers still refuse
  every change and deletion, except a deletion made by their owner while it erases that
  row's workspace (a transaction setting only that function makes; the application's role
  is not the owner, so the setting alone does nothing for it).
- `docforge_delete_document` (0026) now also records that the document's stored files are to
  be erased. The row stays, hidden, as before.
- `erasures` lists what is to be erased from storage until it is; the application cannot
  read it, and clears one entry with `docforge_erasure_done` once the files are gone.

Revision ID: 0033
Revises: 0032
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_SETTINGS = "SET search_path = pg_catalog, public, pg_temp"
# A deletion is let through only by the table's owner, while erasing that row's workspace.
_ERASING = (
    "current_setting('docforge.erasing', true) = OLD.tenant_id::text "
    "AND pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), "
    "'USAGE')"
)

_FORBID = f"""
CREATE OR REPLACE FUNCTION docforge_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND TG_LEVEL = 'ROW' THEN
        IF {_ERASING} THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$
"""
_FORBID_BEFORE = """
CREATE OR REPLACE FUNCTION docforge_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$
"""
_GUARD = """
CREATE OR REPLACE FUNCTION docforge_reviewer_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ERASING THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'reviewers are deactivated, not deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.email <> OLD.email
        OR NEW.name <> OLD.name OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'a reviewer''s identity cannot be changed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

# Children before parents. Messages go with their conversations, agent calls with their keys,
# and a knowledge base's documents with it.
_ERASE_ROWS = """
    DELETE FROM conversations WHERE tenant_id = t;
    DELETE FROM collection_documents WHERE tenant_id = t;
    DELETE FROM collections WHERE tenant_id = t;
    DELETE FROM review_claims WHERE tenant_id = t;
    DELETE FROM chunks WHERE tenant_id = t;
    DELETE FROM reviews WHERE tenant_id = t;
    DELETE FROM corrections WHERE tenant_id = t;
    DELETE FROM matches WHERE tenant_id = t;
    DELETE FROM assessments WHERE tenant_id = t;
    DELETE FROM model_runs WHERE tenant_id = t;
    DELETE FROM extractions WHERE tenant_id = t;
    DELETE FROM parse_outputs WHERE tenant_id = t;
    UPDATE documents SET indexed_version_id = NULL WHERE tenant_id = t;
    DELETE FROM document_versions WHERE tenant_id = t;
    DELETE FROM documents WHERE tenant_id = t;
    DELETE FROM webhook_deliveries WHERE tenant_id = t;
    DELETE FROM webhooks WHERE tenant_id = t;
    DELETE FROM api_keys WHERE tenant_id = t;
    DELETE FROM sessions WHERE tenant_id = t;
    DELETE FROM audit_log WHERE tenant_id = t;
    DELETE FROM accounts WHERE tenant_id = t;
    DELETE FROM reviewers WHERE tenant_id = t;
    -- Counts and places held under keys that name the workspace.
    DELETE FROM rate_windows WHERE strpos(key, t::text) > 0;
    DELETE FROM leases WHERE strpos(key, t::text) > 0;
    DELETE FROM erasures WHERE tenant_id = t;
    DELETE FROM tenants WHERE id = t;
"""

_DELETE_ACCOUNT = "docforge_delete_account()"
_DELETE_ACCOUNT_BODY = f"""RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER {_SETTINGS}
AS $$
DECLARE
    t uuid := {_TENANT};
    erasure bigint;
BEGIN
    -- Locked first: a write into the workspace waits for this, then finds it gone.
    PERFORM 1 FROM tenants WHERE id = t AND kind = 'personal' FOR UPDATE;
    IF t IS NULL OR NOT FOUND THEN
        RAISE EXCEPTION 'only a personal workspace is deleted, and only by itself'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM set_config('docforge.erasing', t::text, true);
    {_ERASE_ROWS}
    PERFORM set_config('docforge.erasing', '', true);
    INSERT INTO erasures (kind, tenant_id) VALUES ('workspace', t) RETURNING id INTO erasure;
    RETURN erasure;
END
$$"""  # noqa: S608 - fixed text, no input in it

_DELETE_DOCUMENT = "docforge_delete_document(document uuid)"
_DELETE_DOCUMENT_BEFORE = (
    f"RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER {_SETTINGS} AS $$ "  # noqa: S608 - fixed text, no input in it
    "UPDATE documents SET deleted_at = now() "
    f"WHERE id = document AND tenant_id = {_TENANT} AND deleted_at IS NULL RETURNING true $$"
)
_DELETE_DOCUMENT_BODY = f"""RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER {_SETTINGS}
AS $$
DECLARE
    t uuid := {_TENANT};
    content text;
    stored text;
    erasure bigint;
BEGIN
    UPDATE documents SET deleted_at = now()
        WHERE id = document AND tenant_id = t AND deleted_at IS NULL
        RETURNING sha256, storage_key INTO content, stored;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    INSERT INTO erasures (kind, tenant_id, sha256, storage_key)
        VALUES ('document', t, content, stored) RETURNING id INTO erasure;
    RETURN erasure;
END
$$"""  # noqa: S608 - fixed text, no input in it

_DONE = "docforge_erasure_done(erasure bigint)"
# A document's, in its workspace; a workspace's, once it is gone.
_DONE_BODY = f"""RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER {_SETTINGS} AS $$
DELETE FROM erasures e WHERE e.id = erasure
    AND (e.tenant_id = {_TENANT} OR NOT EXISTS (SELECT 1 FROM tenants WHERE id = e.tenant_id))
$$"""  # noqa: S608 - fixed text, no input in it


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "erasures",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        # No foreign key: a workspace's erasure outlives the workspace.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.Text),
        sa.Column("storage_key", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("kind IN ('document', 'workspace')", name="ck_erasures_kind"),
        sa.CheckConstraint(
            "kind = 'workspace' OR (sha256 IS NOT NULL AND storage_key IS NOT NULL)",
            name="ck_erasures_document",
        ),
    )
    # Never read by the application itself: no rights, and a policy that admits nothing.
    op.execute(f"REVOKE ALL ON erasures FROM {_APP_ROLE}")
    op.execute("ALTER TABLE erasures ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY erasures_none ON erasures TO {_APP_ROLE} USING (false)")

    op.execute(_FORBID)
    op.execute(_GUARD.replace("ERASING", _ERASING))

    op.execute(f"DROP FUNCTION {_DELETE_DOCUMENT}")
    for signature, body in (
        (_DELETE_DOCUMENT, _DELETE_DOCUMENT_BODY),
        (_DELETE_ACCOUNT, _DELETE_ACCOUNT_BODY),
        (_DONE, _DONE_BODY),
    ):
        op.execute(f"CREATE FUNCTION {signature} {body}")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for signature in (_DONE, _DELETE_ACCOUNT, _DELETE_DOCUMENT):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute(f"CREATE FUNCTION {_DELETE_DOCUMENT} {_DELETE_DOCUMENT_BEFORE}")
    op.execute(f"REVOKE ALL ON FUNCTION {_DELETE_DOCUMENT} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_DELETE_DOCUMENT} TO {_APP_ROLE}")
    op.execute(_GUARD.replace("ERASING", "false"))
    op.execute(_FORBID_BEFORE)
    op.drop_table("erasures")
