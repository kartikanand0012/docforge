"""Migrations run against the real Compose Postgres (pgvector image)."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import IntegrityError

from docforge.db import alembic_config

pytestmark = pytest.mark.integration

CORE_TABLES = {"tenants", "documents", "document_versions"}
SHA = "a" * 64


@pytest.fixture
def migrated(empty_database_url: URL) -> Engine:
    command.upgrade(alembic_config(empty_database_url), "head")
    return create_engine(empty_database_url)


def table_names(url: URL) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def insert_tenant(engine: Engine, name: str) -> uuid.UUID:
    with engine.begin() as conn:
        tenant_id: uuid.UUID = conn.execute(
            text("INSERT INTO tenants (name) VALUES (:name) RETURNING id"), {"name": name}
        ).scalar_one()
    return tenant_id


def insert_document(engine: Engine, tenant_id: uuid.UUID, sha256: str = SHA) -> uuid.UUID:
    with engine.begin() as conn:
        document_id: uuid.UUID = conn.execute(
            text(
                "INSERT INTO documents (tenant_id, sha256, storage_key, filename) "
                "VALUES (:tenant_id, :sha256, :key, 'invoice.pdf') RETURNING id"
            ),
            {"tenant_id": tenant_id, "sha256": sha256, "key": f"{tenant_id}/{sha256}"},
        ).scalar_one()
    return document_id


def test_upgrade_creates_core_tables(migrated: Engine) -> None:
    assert CORE_TABLES <= set(inspect(migrated).get_table_names())


def test_upgrade_enables_pgvector(migrated: Engine) -> None:
    with migrated.connect() as conn:
        distance = conn.execute(text("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")).scalar_one()
    assert distance == pytest.approx(1.0)


def test_downgrade_removes_core_tables_and_upgrade_restores_them(empty_database_url: URL) -> None:
    config = alembic_config(empty_database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert not CORE_TABLES & table_names(empty_database_url)

    command.upgrade(config, "head")
    assert CORE_TABLES <= table_names(empty_database_url)


def test_new_document_starts_in_received_status(migrated: Engine) -> None:
    document_id = insert_document(migrated, insert_tenant(migrated, "acme"))

    with migrated.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM documents WHERE id = :id"), {"id": document_id}
        ).scalar_one()
    assert status == "received"


def test_same_content_hash_is_rejected_within_a_tenant(migrated: Engine) -> None:
    tenant_id = insert_tenant(migrated, "acme")
    insert_document(migrated, tenant_id)

    with pytest.raises(IntegrityError):
        insert_document(migrated, tenant_id)


def test_same_content_hash_is_allowed_across_tenants(migrated: Engine) -> None:
    first = insert_document(migrated, insert_tenant(migrated, "acme"))
    second = insert_document(migrated, insert_tenant(migrated, "globex"))

    assert first != second


def test_malformed_sha256_is_rejected(migrated: Engine) -> None:
    tenant_id = insert_tenant(migrated, "acme")

    with pytest.raises(IntegrityError):
        insert_document(migrated, tenant_id, sha256="not-a-hash")


def test_document_requires_an_existing_tenant(migrated: Engine) -> None:
    with pytest.raises(IntegrityError):
        insert_document(migrated, uuid.uuid4())


def test_version_numbers_are_unique_per_document(migrated: Engine) -> None:
    document_id = insert_document(migrated, insert_tenant(migrated, "acme"))
    insert_version = text(
        "INSERT INTO document_versions (document_id, version_no) VALUES (:id, 1)"
    )
    with migrated.begin() as conn:
        conn.execute(insert_version, {"id": document_id})

    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(insert_version, {"id": document_id})
