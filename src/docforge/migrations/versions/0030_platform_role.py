"""The platform administrator's figures behind a database login of their own.

The functions of migration 0028 return every workspace's figures, and the names and emails
of the people who own them. Until now the application's role could call them, so a flaw in
the application (an injected query) could read them all. They now belong to
`docforge_platform`, a role that can do nothing else: no tables, no other functions. Its
login (`python -m docforge.db.roles`, from PLATFORM_DATABASE_URL) is held by the API alone
and used only after it has checked that the caller is a platform administrator. The check of
0028 inside each function (a setting the caller makes in its transaction) stays as well.

Revision ID: 0030
Revises: 0029
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "docforge_app"
PLATFORM_ROLE = "docforge_platform"
PLATFORM_FUNCTIONS = (
    "docforge_platform_workspaces()",
    "docforge_platform_usage()",
    "docforge_platform_accounts()",
    "docforge_platform_activity(before_id bigint, max_rows integer, only_tenant uuid)",
)

_CREATE_ROLE = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PLATFORM_ROLE}') THEN
        CREATE ROLE {PLATFORM_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;
"""  # noqa: S608 - fixed text, no input in it


def upgrade() -> None:
    # Roles belong to the cluster: made once, and only its rights in this database are set.
    op.execute(_CREATE_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {PLATFORM_ROLE}")
    for signature in PLATFORM_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, {_APP_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {PLATFORM_ROLE}")


def downgrade() -> None:
    for signature in PLATFORM_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM {PLATFORM_ROLE}")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP_ROLE}")
    # The role may serve other databases on the cluster, so only its rights here are removed.
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {PLATFORM_ROLE}")
