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
                "INSERT INTO documents "
                "(tenant_id, doc_type, sha256, storage_key, filename, size_bytes) "
                "VALUES (:tenant_id, 'invoice', :sha256, :key, 'invoice.pdf', 1) RETURNING id"
            ),
            {"tenant_id": tenant_id, "sha256": sha256, "key": f"{tenant_id}/{sha256}"},
        ).scalar_one()
    return document_id


def test_upgrade_creates_core_tables(migrated: Engine) -> None:
    assert set(inspect(migrated).get_table_names()) >= CORE_TABLES


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
    assert table_names(empty_database_url) >= CORE_TABLES


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
        "INSERT INTO document_versions (tenant_id, document_id, version_no) "
        "SELECT tenant_id, id, 1 FROM documents WHERE id = :id"
    )
    with migrated.begin() as conn:
        conn.execute(insert_version, {"id": document_id})

    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(insert_version, {"id": document_id})


# Downgrades that would lose or contradict data refuse unless told


ALLOW = "DOCFORGE_ALLOW_DESTRUCTIVE_DOWNGRADE"


def make_account(engine: Engine) -> None:
    from docforge.review.signing import hash_pin

    with engine.begin() as conn:
        conn.execute(
            text("SELECT docforge_create_account('u-0123456789ab', 'A', 'A', 'a@example.com', :h)"),
            {"h": hash_pin("correct horse battery staple")},
        )


def test_removing_accounts_refuses_while_accounts_exist_unless_told(
    empty_database_url: URL, migrated: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_account(migrated)
    config = alembic_config(empty_database_url)
    monkeypatch.delenv(ALLOW, raising=False)

    with pytest.raises(RuntimeError, match=ALLOW):
        command.downgrade(config, "0026")
    with migrated.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM accounts")).scalar_one() == 1

    monkeypatch.setenv(ALLOW, "1")
    command.downgrade(config, "0026")
    with migrated.connect() as conn:
        role = conn.execute(
            text("SELECT role FROM reviewers WHERE email = 'a@example.com'")
        ).scalar_one()
    assert role == "reviewer"  # its account is gone; the person stays, as a plain reviewer


def test_the_one_document_per_content_rule_is_not_restored_over_duplicates(
    empty_database_url: URL, migrated: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = insert_tenant(migrated, "acme")
    first = insert_document(migrated, tenant_id)
    with migrated.begin() as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": first})
    insert_document(migrated, tenant_id)  # the same bytes uploaded again
    monkeypatch.setenv(ALLOW, "1")

    with pytest.raises(RuntimeError, match="same content"):
        command.downgrade(alembic_config(empty_database_url), "0025")


# Indexes for the counts every upload and the screens make


def test_the_readings_index_and_count_name_exactly_the_actions_that_are_readings(
    migrated: Engine,
) -> None:
    import re

    from docforge.documents import READINGS

    with migrated.connect() as conn:
        index = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_audit_log_readings'")
        ).scalar_one()
        counted = conn.execute(
            text("SELECT pg_get_functiondef('docforge_member_readings_since'::regproc)")
        ).scalar_one()
    in_index = set(re.findall(r"'(document\.[a-z_]+)'", index))
    in_count = set(re.findall(r"'(document\.[a-z_]+)'", counted))
    assert in_index == in_count == set(READINGS)
    assert "(occurred_at, tenant_id)" in index


def test_the_audit_and_sign_in_figures_have_their_indexes(migrated: Engine) -> None:
    with migrated.connect() as conn:
        found = dict(
            conn.execute(
                text(
                    "SELECT c.relname, i.indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname IN "
                    "('ix_audit_log_action_time', 'ix_sessions_tenant_created')"
                )
            ).all()
        )
        action_time = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_audit_log_action_time'")
        ).scalar_one()
    assert found == {"ix_audit_log_action_time": True, "ix_sessions_tenant_created": True}
    assert "(tenant_id, action, occurred_at) INCLUDE (actor)" in action_time
