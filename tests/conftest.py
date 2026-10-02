"""Shared fixtures."""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from docforge.config import Settings, get_settings

TEST_DATABASE = "docforge_test"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def empty_database_url(settings: Settings) -> Iterator[URL]:
    """A freshly created, empty database on the Compose Postgres, dropped afterwards."""
    admin_url = make_url(settings.database_url.get_secret_value())
    if admin_url.host not in LOCAL_HOSTS:
        # These tests create and drop a database; never do that on a shared server.
        pytest.fail(f"integration tests only run against a local Postgres, not {admin_url.host}")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DATABASE}"'))
    try:
        yield admin_url.set(database=TEST_DATABASE)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)'))
        admin.dispose()
