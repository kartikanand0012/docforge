"""Fixtures for tests that use the Compose Postgres with all migrations applied."""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import URL, Engine

from docforge.db import alembic_config
from docforge.db.session import SessionFactory, make_engine, make_session_factory


@pytest.fixture
def engine(empty_database_url: URL) -> Iterator[Engine]:
    command.upgrade(alembic_config(empty_database_url), "head")
    engine = make_engine(empty_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine: Engine) -> SessionFactory:
    return make_session_factory(engine)


@pytest.fixture
def other_tenant(engine: Engine) -> uuid.UUID:
    with engine.begin() as conn:
        tenant_id: uuid.UUID = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('other') RETURNING id")
        ).scalar_one()
    return tenant_id
