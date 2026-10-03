"""A migrated database of its own for an eval or a measurement, dropped afterwards."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from docforge.config import get_settings
from docforge.db import alembic_config
from docforge.db.roles import ensure_app_login

_LOCAL = {"127.0.0.1", "localhost", "::1"}


@contextmanager
def temporary_database(prefix: str, database_url: Any = None) -> Iterator[tuple[URL, URL]]:
    """(owner URL, application URL) of a migrated database: a new one named after `prefix`,
    or `database_url` if given. Local Postgres only: these runs create and drop databases."""
    settings = get_settings()
    owner = make_url(settings.migration_database_url.get_secret_value())
    temporary = database_url is None
    # A name of its own, so two runs at once (CI workers, a developer) never drop each other's.
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    url = owner.set(database=name) if database_url is None else make_url(database_url)
    if url.host not in _LOCAL:
        raise RuntimeError("this only runs against a local Postgres")
    admin = create_engine(owner, isolation_level="AUTOCOMMIT")
    try:
        if temporary:
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
        command.upgrade(alembic_config(url), "head")
        app = make_url(settings.database_url.get_secret_value())
        ensure_app_login(owner, app)
        yield url, url.set(username=app.username, password=app.password)
    finally:
        if temporary:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
