"""Row-level security per tenant, and the restricted role the application connects as.

Revision ID: 0008
Revises: 0007

Every table with a `tenant_id` gets a policy: a row is visible and writable only when it
belongs to the tenant in the `docforge.tenant_id` setting, which the application sets per
transaction (`docforge.db.tenancy`). The owner, who runs migrations, is not subject to the
policies; the application's role is, and holds only SELECT, INSERT and UPDATE: no DELETE, no
TRUNCATE, no ownership, so it cannot switch the append-only triggers or the policies off.

Three functions run as the owner to find a tenant before it is known: from an API key's or
a session's prefix, and from a document version (the worker's job carries only that). Each
returns a tenant id and nothing else.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "docforge_app"
TENANT_TABLES = (
    "documents",
    "document_versions",
    "parse_outputs",
    "extractions",
    "model_runs",
    "assessments",
    "matches",
    "audit_log",
    "reviewers",
    "corrections",
    "reviews",
    "api_keys",
    "sessions",
)
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"

_LOOKUPS = {
    "docforge_api_key_tenant(key_prefix text)": (
        "SELECT tenant_id FROM api_keys WHERE prefix = key_prefix AND revoked_at IS NULL"
    ),
    "docforge_session_tenant(session_prefix text)": (
        "SELECT tenant_id FROM sessions WHERE prefix = session_prefix AND revoked_at IS NULL "
        "AND expires_at > now()"
    ),
    "docforge_version_tenant(version uuid)": (
        "SELECT tenant_id FROM document_versions WHERE id = version"
    ),
}


_CREATE_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'APP_ROLE') THEN
        CREATE ROLE APP_ROLE NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;
""".replace("APP_ROLE", APP_ROLE)

_GRANT_QUEUE = """
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND tablename LIKE 'procrastinate%' LOOP
        EXECUTE format('GRANT ALL ON TABLE %I TO APP_ROLE', item.tablename);
    END LOOP;
END
$$;
""".replace("APP_ROLE", APP_ROLE)


def upgrade() -> None:
    # Roles belong to the cluster, not this database: create it once, use it everywhere.
    op.execute(_CREATE_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # The queue's own tables and functions: the worker claims, updates and deletes its jobs.
    op.execute(_GRANT_QUEUE)
    op.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {APP_ROLE}")
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} TO {APP_ROLE} "
            f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
        )
    for signature, query in _LOOKUPS.items():
        op.execute(
            f"CREATE FUNCTION {signature} RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER "
            f"SET search_path = public, pg_temp AS $$ {query} $$"
        )
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {APP_ROLE}")


def downgrade() -> None:
    for signature in _LOOKUPS:
        op.execute(f"DROP FUNCTION {signature.split('(')[0]}")
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY {table}_tenant ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    # The role may serve other databases on the cluster, so only its rights here are removed.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
