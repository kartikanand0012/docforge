"""`python -m docforge.db.roles`: create or update the application's database login.

    python -m docforge.db.roles

Run as the owner (MIGRATION_DATABASE_URL) after `alembic upgrade head`. It makes the login
named in DATABASE_URL a member of `docforge_app`, the role migration 0008 grants rights to,
with the password in DATABASE_URL. The login can then read and write rows of the tenant it
is told about, and nothing else.
"""

import sys

from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

from docforge.config import get_settings

APP_ROLE = "docforge_app"


def ensure_app_login(owner_url: URL | str, app_url: URL | str) -> str:
    """Create or update the login in `app_url`, as a member of `docforge_app`. Returns its name."""
    app = make_url(app_url)
    if not app.username or not app.password:
        raise ValueError("DATABASE_URL must name a user and a password")
    engine = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            raw = conn.connection.driver_connection
            assert raw is not None  # noqa: S101
            with raw.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app.username,))
                verb = "ALTER" if cursor.fetchone() else "CREATE"
                cursor.execute(
                    sql.SQL(
                        "{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOBYPASSRLS NOREPLICATION"
                    ).format(sql.SQL(verb), sql.Identifier(app.username), sql.Literal(app.password))
                )
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(APP_ROLE), sql.Identifier(app.username)
                    )
                )
    finally:
        engine.dispose()
    return app.username


def main() -> int:
    settings = get_settings()
    try:
        name = ensure_app_login(
            settings.migration_database_url.get_secret_value(),
            settings.database_url.get_secret_value(),
        )
    except ValueError as error:
        print(f"Not done: {error}", file=sys.stderr)
        return 1
    print(f"Database login {name} is ready (member of {APP_ROLE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
