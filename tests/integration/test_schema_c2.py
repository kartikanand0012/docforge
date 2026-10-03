"""Migration 0002: durable processing tables and the rules the database itself enforces."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from docforge.db import DEFAULT_TENANT_ID, alembic_config

pytestmark = pytest.mark.integration

NEW_TABLES = {"parse_outputs", "extractions", "model_runs", "audit_log"}
SHA = "b" * 64


@pytest.fixture
def db(empty_database_url: URL) -> Engine:
    command.upgrade(alembic_config(empty_database_url), "head")
    return create_engine(empty_database_url)


def scalar(engine: Engine, sql: str, **params: object) -> object:
    with engine.begin() as conn:
        return conn.execute(text(sql), params).scalar_one()


def new_version(engine: Engine) -> uuid.UUID:
    document_id = scalar(
        engine,
        "INSERT INTO documents (tenant_id, doc_type, sha256, storage_key, filename, size_bytes) "
        "VALUES (:tenant, 'invoice', :sha, 'k', 'a.pdf', 10) RETURNING id",
        tenant=DEFAULT_TENANT_ID,
        sha=SHA,
    )
    version_id = scalar(
        engine,
        "INSERT INTO document_versions (tenant_id, document_id, version_no) "
        "VALUES (:tenant, :document, 1) RETURNING id",
        tenant=DEFAULT_TENANT_ID,
        document=document_id,
    )
    assert isinstance(version_id, uuid.UUID)
    return version_id


def new_extraction(engine: Engine, version_id: uuid.UUID) -> object:
    return scalar(
        engine,
        "INSERT INTO extractions (tenant_id, document_version_id, schema_version, data, sha256) "
        "VALUES (:tenant, :version, 'invoice-1', '{}'::jsonb, :sha) RETURNING id",
        tenant=DEFAULT_TENANT_ID,
        version=version_id,
        sha=SHA,
    )


def append_audit(engine: Engine, digit: str = "1", after: str | None = None) -> object:
    return scalar(
        engine,
        "INSERT INTO audit_log (tenant_id, occurred_at, actor, action, target_type, target_id, "
        "details, prev_hash, hash) VALUES (:tenant, now(), 'test', 'x', 'document', 'id', "
        "'{}'::jsonb, :prev, :hash) RETURNING id",
        tenant=DEFAULT_TENANT_ID,
        prev=after * 64 if after else None,
        hash=digit * 64,
    )


def test_creates_the_processing_tables_and_the_queue(db: Engine) -> None:
    tables = set(inspect(db).get_table_names())

    assert tables >= NEW_TABLES
    assert "procrastinate_jobs" in tables


def test_a_default_tenant_exists(db: Engine) -> None:
    assert scalar(db, "SELECT name FROM tenants WHERE id = :id", id=DEFAULT_TENANT_ID) == "default"


def test_downgrade_to_0001_removes_everything_it_added(empty_database_url: URL) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "head")

    command.downgrade(config, "0001")

    engine = create_engine(empty_database_url)
    tables = set(inspect(engine).get_table_names())
    assert not tables & NEW_TABLES
    assert not {name for name in tables if name.startswith("procrastinate")}
    leftovers = scalar(
        engine,
        "SELECT count(*) FROM pg_proc WHERE proname LIKE 'procrastinate%' "
        "OR proname LIKE 'docforge%'",
    )
    assert leftovers == 0
    command.upgrade(config, "head")  # and it can be applied again
    engine.dispose()


def test_new_versions_start_queued_and_documents_received(db: Engine) -> None:
    version_id = new_version(db)

    assert scalar(db, "SELECT status FROM document_versions WHERE id = :id", id=version_id) == (
        "queued"
    )
    assert scalar(db, "SELECT status FROM documents WHERE sha256 = :sha", sha=SHA) == "received"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE documents SET status = 'done'",
        "UPDATE document_versions SET status = 'done'",
        "UPDATE document_versions SET version_no = 0",
    ],
)
def test_unknown_statuses_and_bad_version_numbers_are_rejected(db: Engine, statement: str) -> None:
    new_version(db)

    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(text(statement))


def test_a_version_has_at_most_one_extraction(db: Engine) -> None:
    version_id = new_version(db)
    new_extraction(db, version_id)

    with pytest.raises(IntegrityError):
        new_extraction(db, version_id)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE extractions SET schema_version = 'x'", "DELETE FROM extractions"],
)
def test_an_extraction_cannot_be_changed_or_deleted(db: Engine, statement: str) -> None:
    new_extraction(db, new_version(db))

    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text(statement))

    assert scalar(db, "SELECT schema_version FROM extractions") == "invoice-1"


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET actor = 'someone else'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
def test_the_audit_log_cannot_be_changed_deleted_or_truncated(db: Engine, statement: str) -> None:
    append_audit(db)

    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text(statement))

    assert scalar(db, "SELECT actor FROM audit_log") == "test"


def test_audit_entries_are_numbered_in_insertion_order(db: Engine) -> None:
    first, second = append_audit(db, "1"), append_audit(db, "2", after="1")

    assert isinstance(first, int)
    assert isinstance(second, int)
    assert second > first


def test_audit_hashes_are_unique(db: Engine) -> None:
    append_audit(db, "1")

    with pytest.raises(IntegrityError):
        append_audit(db, "1")


def test_extractions_cannot_be_truncated(db: Engine) -> None:
    new_extraction(db, new_version(db))

    # Refused by the append-only trigger, or earlier, since C5, because reviews reference it.
    with (
        pytest.raises(DBAPIError, match="append-only|referenced in a foreign key"),
        db.begin() as conn,
    ):
        conn.execute(text("TRUNCATE extractions"))


def test_append_only_triggers_fire_even_in_replica_mode(db: Engine) -> None:
    modes = scalar(
        db,
        "SELECT string_agg(DISTINCT tgenabled::text, '') FROM pg_trigger "
        "WHERE tgname LIKE '%append_only' OR tgname LIKE '%no_truncate'",
    )

    assert modes == "A"  # ALWAYS: session_replication_role = replica does not skip them


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO audit_log (tenant_id, occurred_at, actor, action, target_type, target_id, "
        "details, hash) VALUES (:tenant, now(), 'a', 'b', 'c', 'd', '{}'::jsonb, 'not-a-hash')",
        "INSERT INTO extractions (tenant_id, document_version_id, schema_version, data, sha256) "
        "SELECT tenant_id, id, 'x', '{}'::jsonb, 'not-a-hash' FROM document_versions",
    ],
)
def test_malformed_hashes_are_rejected(db: Engine, statement: str) -> None:
    new_version(db)

    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(text(statement), {"tenant": DEFAULT_TENANT_ID})


def test_tenant_columns_are_indexed_for_row_level_security(db: Engine) -> None:
    for table in ("document_versions", "parse_outputs", "extractions", "model_runs"):
        leading = {index["column_names"][0] for index in inspect(db).get_indexes(table)}
        assert "tenant_id" in leading, table


def test_downgrade_refuses_to_discard_audit_records(empty_database_url: URL) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "head")
    engine = create_engine(empty_database_url)
    append_audit(engine)

    with pytest.raises(Exception, match="audit_log"):
        command.downgrade(config, "0001")

    assert scalar(engine, "SELECT count(*) FROM audit_log") == 1
    engine.dispose()


def test_upgrade_works_on_a_database_that_already_has_documents(empty_database_url: URL) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "0001")
    engine = create_engine(empty_database_url)
    tenant = scalar(engine, "INSERT INTO tenants (name) VALUES ('early') RETURNING id")
    document = scalar(
        engine,
        "INSERT INTO documents (tenant_id, sha256, storage_key, filename) "
        "VALUES (:tenant, :sha, 'k', 'old.pdf') RETURNING id",
        tenant=tenant,
        sha=SHA,
    )
    scalar(
        engine,
        "INSERT INTO document_versions (document_id, version_no) VALUES (:document, 1) "
        "RETURNING id",
        document=document,
    )

    command.upgrade(config, "head")

    assert scalar(engine, "SELECT tenant_id FROM document_versions") == tenant
    assert scalar(engine, "SELECT doc_type FROM documents") == "invoice"
    # No job exists for a version created before the queue did, so it must not look queued.
    assert scalar(engine, "SELECT status FROM document_versions") == "failed"
    engine.dispose()
