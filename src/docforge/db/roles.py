"""`python -m docforge.db.roles`: create or update the application's database logins.

    python -m docforge.db.roles

Run as the owner (MIGRATION_DATABASE_URL) after `alembic upgrade head`. It makes the login
named in DATABASE_URL a member of `docforge_app`, the role migration 0008 grants rights to,
with the password in DATABASE_URL. The login can then read and write rows of the tenant it
is told about, and nothing else.

When PLATFORM_DATABASE_URL is set, it also makes the login named there a member of
`docforge_platform` (migration 0030), with the password there: it may call the platform
administrator's functions across workspaces, and nothing else. The two logins must differ.
"""

import sys

from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

from docforge.config import get_settings

APP_ROLE = "docforge_app"
PLATFORM_ROLE = "docforge_platform"
_ROLES = (APP_ROLE, PLATFORM_ROLE)


def ensure_login(owner_url: URL | str, login_url: URL | str, role: str, setting: str) -> str:
    """Create or update the login in `login_url` as a member of `role`, and of no other of
    DocForge's roles. Returns its name."""
    if role not in _ROLES:
        raise ValueError(f"unknown role {role!r}")
    login = make_url(login_url)
    if not login.username or not login.password:
        raise ValueError(f"{setting} must name a user and a password")
    engine = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            raw = conn.connection.driver_connection
            assert raw is not None  # noqa: S101
            with raw.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login.username,))
                verb = "ALTER" if cursor.fetchone() else "CREATE"
                cursor.execute(
                    sql.SQL(
                        "{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOBYPASSRLS NOREPLICATION"
                    ).format(
                        sql.SQL(verb), sql.Identifier(login.username), sql.Literal(login.password)
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(role), sql.Identifier(login.username)
                    )
                )
                # One login, one role: the application's never reads across workspaces, and
                # the platform's never reads a workspace's rows.
                for other in _ROLES:
                    if other != role:  # a no-op, with a notice, when it is not a member
                        cursor.execute(
                            sql.SQL("REVOKE {} FROM {}").format(
                                sql.Identifier(other), sql.Identifier(login.username)
                            )
                        )
    finally:
        engine.dispose()
    return login.username


def ensure_app_login(owner_url: URL | str, app_url: URL | str) -> str:
    """Create or update the login in `app_url`, as a member of `docforge_app`. Returns its name."""
    return ensure_login(owner_url, app_url, APP_ROLE, "DATABASE_URL")


def ensure_platform_login(owner_url: URL | str, platform_url: URL | str) -> str:
    """Create or update the login in `platform_url`, as a member of `docforge_platform`."""
    return ensure_login(owner_url, platform_url, PLATFORM_ROLE, "PLATFORM_DATABASE_URL")


def main() -> int:
    settings = get_settings()
    owner = settings.migration_database_url.get_secret_value()
    try:
        name = ensure_app_login(owner, settings.database_url.get_secret_value())
        print(f"Database login {name} is ready (member of {APP_ROLE})")
        if settings.platform_database_url is not None:
            name = ensure_platform_login(owner, settings.platform_database_url.get_secret_value())
            print(f"Database login {name} is ready (member of {PLATFORM_ROLE})")
    except ValueError as error:
        print(f"Not done: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
