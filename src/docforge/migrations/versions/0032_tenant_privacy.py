"""Workspaces under row-level security: the application's role sees only its own.

A personal workspace's display name holds its person's name, so `tenants` is now read like
every other table: only the row of the tenant in the `docforge.tenant_id` setting. What is
looked up before a workspace is known goes through functions that run as the owner and
return one thing each:

- `docforge_organisation_tenant(name)`: the id of the organisation with that name, for
  signing in with an organisation's name (personal workspaces are not found by name);
- `docforge_tenant_ids()`: every workspace's id and nothing else, for anchoring each audit
  chain.

Revision ID: 0032
Revises: 0031
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_TENANT = "nullif(current_setting('docforge.tenant_id', true), '')::uuid"
_DEFINER = "LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp"
_FUNCTIONS = {
    "docforge_organisation_tenant(organisation text)": (
        "RETURNS uuid",
        "SELECT id FROM tenants WHERE name = organisation AND kind = 'organisation'",
    ),
    "docforge_tenant_ids()": ("RETURNS SETOF uuid", "SELECT id FROM tenants ORDER BY id"),
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenants_own ON tenants FOR SELECT TO {_APP_ROLE} USING (id = {_TENANT})"
    )
    for signature, (returns, body) in _FUNCTIONS.items():
        op.execute(f"CREATE FUNCTION {signature} {returns} {_DEFINER} AS $$ {body} $$")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for signature in _FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature.split('(')[0]}")
    op.execute("DROP POLICY tenants_own ON tenants")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
