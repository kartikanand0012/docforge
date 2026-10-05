"""Row-level security: the application's database role sees only the current tenant's rows,
and cannot lift that or change what the append-only tables hold."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    world.process("purchase_order")
    world.process("invoice")
    return world


def count(engine: Engine, table: str, tenant: uuid.UUID | None) -> int:
    with engine.begin() as conn:
        if tenant is not None:
            conn.execute(
                text("SELECT set_config('docforge.tenant_id', :t, true)"), {"t": str(tenant)}
            )
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def tenant_tables(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'tenant_id' ORDER BY 1"
                )
            ).scalars()
        )


def test_every_table_with_a_tenant_has_row_level_security(owner_engine: Engine) -> None:
    tables = tenant_tables(owner_engine)
    with owner_engine.connect() as conn:
        protected = set(
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_policy p ON p.polrelid = c.oid "
                    "WHERE c.relrowsecurity"
                )
            ).scalars()
        )

    assert len(tables) >= 13
    assert set(tables) <= protected, set(tables) - protected


def test_without_a_tenant_the_application_sees_nothing(
    world: World, engine: Engine, owner_engine: Engine
) -> None:
    for table in ("documents", "extractions", "assessments", "matches", "audit_log", "reviewers"):
        assert count(engine, table, None) == 0, table
    assert count(owner_engine, "documents", None) == 2


def test_with_a_tenant_the_application_sees_only_that_tenant(
    world: World, engine: Engine, other_tenant: uuid.UUID
) -> None:
    for table in ("documents", "document_versions", "extractions", "audit_log"):
        assert count(engine, table, DEFAULT_TENANT_ID) > 0, table
        assert count(engine, table, other_tenant) == 0, table


def test_a_row_for_another_tenant_cannot_be_written(
    engine: Engine, other_tenant: uuid.UUID
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"), engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('docforge.tenant_id', :t, true)"), {"t": str(DEFAULT_TENANT_ID)}
        )
        conn.execute(
            text(
                "INSERT INTO documents (tenant_id, doc_type, sha256, storage_key, filename, "
                "size_bytes) VALUES (:t, 'invoice', :sha, 'k', 'a.pdf', 1)"
            ),
            {"t": other_tenant, "sha": "c" * 64},
        )


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM documents",
        "TRUNCATE extractions",
        "ALTER TABLE extractions DISABLE TRIGGER extractions_append_only",
        "ALTER TABLE documents DISABLE ROW LEVEL SECURITY",
        "SET ROLE docforge",
        "DROP TABLE reviews",
    ],
)
def test_the_application_role_cannot_lift_the_protections(engine: Engine, statement: str) -> None:
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('docforge.tenant_id', :t, true)"), {"t": str(DEFAULT_TENANT_ID)}
        )
        conn.execute(text(statement))


def test_the_application_role_neither_bypasses_security_nor_owns_tables(engine: Engine) -> None:
    with engine.connect() as conn:
        role = conn.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
        owned = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'public' AND tableowner = current_user"
            )
        ).scalar_one()

    assert tuple(role) == (False, False, False)
    assert owned == 0


def test_credentials_are_found_by_prefix_without_reading_other_tenants_rows(
    engine: Engine,
) -> None:
    with engine.connect() as conn:
        missing = conn.execute(text("SELECT docforge_api_key_tenant('000000000000')")).scalar_one()
        visible = conn.execute(text("SELECT count(*) FROM api_keys")).scalar_one()

    assert missing is None
    assert visible == 0
