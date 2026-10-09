"""Free workspaces' readings today, across every one of them: a deployment-wide safety valve.

Each upload and each reading again is a paid model reading. A free workspace is held to its
own count a day; `docforge_member_readings_since` counts them across every personal
workspace, which the application's role (one workspace at a time) cannot. It returns one
number and nothing else, runs as the owner with its search path pinned, and only the
application may call it.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
_SIGNATURE = "docforge_member_readings_since(since timestamptz)"
_BODY = (
    "RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER "
    "SET search_path = pg_catalog, public, pg_temp AS $$ "
    "SELECT count(*) FROM audit_log a JOIN tenants t ON t.id = a.tenant_id "
    "WHERE t.kind = 'personal' AND a.occurred_at >= since "
    "AND a.action IN ('document.received', 'document.reprocess_requested') $$"
)


def upgrade() -> None:
    op.execute(f"CREATE FUNCTION {_SIGNATURE} {_BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION {_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
