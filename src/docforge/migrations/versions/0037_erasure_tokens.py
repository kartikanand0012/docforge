"""Erasure, tightened: a token per erasure, and a single-use grant to delete an account.

- Each recorded erasure has a random `token`. `docforge_delete_document` and
  `docforge_delete_account` return it with the erasure's id, and `docforge_erasure_done(id,
  token)` clears an erasure only when both match: another workspace cannot cancel one by
  guessing its sequential id, which left its files unerased.
- `deletion_grants`: once the API has checked the person's password again, the owner's
  `docforge_grant_deletion(reviewer)` records a grant for the workspace in scope and its own
  member, good for 60 seconds. `docforge_delete_account(grant)` deletes the workspace only
  with such a grant, unused and unexpired, and uses it up. The application cannot read or
  write the grants itself. A workspace's grants go with it.

Revision ID: 0037
Revises: 0036
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_SETTINGS = "SET search_path = pg_catalog, public, pg_temp"
_RETURNS = "RETURNS TABLE (erasure_id bigint, erasure_token uuid)"

# Children before parents, as 0033 and 0036 had them.
_ERASE_ROWS = """
    DELETE FROM question_usage WHERE tenant_id = t;
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
    DELETE FROM rate_windows WHERE strpos(key, t::text) > 0;
    DELETE FROM leases WHERE strpos(key, t::text) > 0;
    DELETE FROM erasures WHERE tenant_id = t;
    DELETE FROM deletion_grants WHERE tenant_id = t;
    DELETE FROM tenants WHERE id = t;
"""

_GRANT = "docforge_grant_deletion(member uuid)"
_GRANT_BODY = f"""RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER {_SETTINGS} AS $$
DECLARE
    t uuid := {_TENANT};
    made uuid;
BEGIN
    IF t IS NULL OR NOT EXISTS (
        SELECT 1 FROM tenants w JOIN accounts a ON a.tenant_id = w.id
        WHERE w.id = t AND w.kind = 'personal' AND a.reviewer_id = member
    ) THEN
        RAISE EXCEPTION 'a deletion grant is only for a personal workspace''s own member'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO deletion_grants (tenant_id, reviewer_id, expires_at)
        VALUES (t, member, now() + interval '60 seconds') RETURNING token INTO made;
    RETURN made;
END
$$"""  # noqa: S608 - fixed text, no input in it

_DELETE_ACCOUNT = "docforge_delete_account(deletion_grant uuid)"
_DELETE_ACCOUNT_BODY = f"""{_RETURNS} LANGUAGE plpgsql VOLATILE SECURITY DEFINER {_SETTINGS}
AS $$
DECLARE
    t uuid := {_TENANT};
BEGIN
    -- Locked first: a write into the workspace waits for this, then finds it gone.
    PERFORM 1 FROM tenants WHERE id = t AND kind = 'personal' FOR UPDATE;
    IF t IS NULL OR NOT FOUND THEN
        RAISE EXCEPTION 'only a personal workspace is deleted, and only by itself'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE deletion_grants SET used_at = now()
        WHERE token = deletion_grant AND tenant_id = t AND used_at IS NULL
            AND expires_at > now();
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no fresh deletion grant for this workspace'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM set_config('docforge.erasing', t::text, true);
    {_ERASE_ROWS}
    PERFORM set_config('docforge.erasing', '', true);
    RETURN QUERY INSERT INTO erasures (kind, tenant_id) VALUES ('workspace', t)
        RETURNING id, token;
END
$$"""  # noqa: S608 - fixed text, no input in it

_DELETE_DOCUMENT = "docforge_delete_document(document uuid)"
_DELETE_DOCUMENT_BODY = f"""{_RETURNS} LANGUAGE plpgsql VOLATILE SECURITY DEFINER {_SETTINGS}
AS $$
DECLARE
    t uuid := {_TENANT};
    content text;
    stored text;
BEGIN
    UPDATE documents SET deleted_at = now()
        WHERE id = document AND tenant_id = t AND deleted_at IS NULL
        RETURNING sha256, storage_key INTO content, stored;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY INSERT INTO erasures (kind, tenant_id, sha256, storage_key)
        VALUES ('document', t, content, stored) RETURNING id, token;
END
$$"""  # noqa: S608 - fixed text, no input in it

_DONE = "docforge_erasure_done(erasure bigint, erasure_token uuid)"
_DONE_BODY = f"""RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER {_SETTINGS} AS $$
DELETE FROM erasures WHERE id = erasure AND token = erasure_token
$$"""  # noqa: S608 - fixed text, no input in it

_OLD = (
    "docforge_delete_account()",
    "docforge_delete_document(document uuid)",
    "docforge_erasure_done(erasure bigint)",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "erasures",
        sa.Column(
            "token",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.create_table(
        "deletion_grants",
        sa.Column(
            "token",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # No foreign keys: a used grant outlives its workspace.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(f"REVOKE ALL ON deletion_grants FROM {_APP_ROLE}")
    op.execute("ALTER TABLE deletion_grants ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY deletion_grants_none ON deletion_grants TO {_APP_ROLE} USING (false)"
    )
    for signature in _OLD:
        op.execute(f"DROP FUNCTION {signature}")
    for signature, body in (
        (_GRANT, _GRANT_BODY),
        (_DELETE_ACCOUNT, _DELETE_ACCOUNT_BODY),
        (_DELETE_DOCUMENT, _DELETE_DOCUMENT_BODY),
        (_DONE, _DONE_BODY),
    ):
        op.execute(f"CREATE FUNCTION {signature} {body}")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")


def downgrade() -> None:
    import importlib.util
    from pathlib import Path

    op.execute("SET LOCAL lock_timeout = '5s'")
    for signature in (_DONE, _DELETE_DOCUMENT, _DELETE_ACCOUNT, _GRANT):
        op.execute(f"DROP FUNCTION {signature}")
    # 0033's functions as they were, then 0036's change to the account's.
    spec = importlib.util.spec_from_file_location(
        "erasure_0033", Path(__file__).with_name("0033_erasure.py")
    )
    assert spec is not None and spec.loader is not None  # noqa: S101
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)
    account = before._DELETE_ACCOUNT_BODY.replace(
        "DELETE FROM conversations WHERE tenant_id = t;",
        "DELETE FROM question_usage WHERE tenant_id = t;\n"
        "    DELETE FROM conversations WHERE tenant_id = t;",
        1,
    )
    for signature, body in (
        (before._DELETE_DOCUMENT, before._DELETE_DOCUMENT_BODY),
        (before._DELETE_ACCOUNT, account),
        (before._DONE, before._DONE_BODY),
    ):
        op.execute(f"CREATE FUNCTION {signature} {body}")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")
    op.drop_table("deletion_grants")
    op.drop_column("erasures", "token")
