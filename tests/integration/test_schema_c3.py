"""Migration 0005: a record cannot point across tenants, and a match has an invoice and an order."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from docforge.db import DEFAULT_TENANT_ID, alembic_config
from docforge.db.models import ORDER_NUMBER, Extraction

pytestmark = pytest.mark.integration


def scalar(engine: Engine, sql: str, **params: object) -> object:
    with engine.begin() as conn:
        return conn.execute(text(sql), params).scalar_one()


def new_version(
    engine: Engine, doc_type: str = "invoice", tenant: uuid.UUID = DEFAULT_TENANT_ID
) -> object:
    document_id = scalar(
        engine,
        "INSERT INTO documents (tenant_id, doc_type, sha256, storage_key, filename, size_bytes) "
        "VALUES (:tenant, :type, :sha, 'k', 'a.pdf', 10) RETURNING id",
        tenant=tenant,
        type=doc_type,
        sha=uuid.uuid4().hex * 2,
    )
    return scalar(
        engine,
        "INSERT INTO document_versions (tenant_id, document_id, version_no) "
        "VALUES (:tenant, :document, 1) RETURNING id",
        tenant=tenant,
        document=document_id,
    )


def new_match(engine: Engine, invoice: object, order: object, tenant: uuid.UUID) -> object:
    return scalar(
        engine,
        "INSERT INTO matches (tenant_id, invoice_version_id, order_version_id, decision, data) "
        "VALUES (:tenant, :invoice, :order, 'match', '{}'::jsonb) RETURNING id",
        tenant=tenant,
        invoice=invoice,
        order=order,
    )


INSERTS = {
    "assessments": "INSERT INTO assessments (tenant_id, document_version_id, decision, data) "
    "VALUES (:tenant, :version, 'accept', '{}'::jsonb) RETURNING id",
    "extractions": "INSERT INTO extractions (tenant_id, document_version_id, schema_version, "
    "data, sha256) VALUES (:tenant, :version, 'invoice-1', '{}'::jsonb, :sha) RETURNING id",
}


@pytest.mark.parametrize("table", sorted(INSERTS))
def test_a_record_cannot_carry_one_tenant_and_point_at_anothers_version(
    engine: Engine, other_tenant: uuid.UUID, table: str
) -> None:
    version = new_version(engine)
    params = (
        {"version": version, "sha": "b" * 64} if table == "extractions" else {"version": version}
    )

    with pytest.raises(IntegrityError):
        scalar(engine, INSERTS[table], tenant=other_tenant, **params)

    assert scalar(engine, INSERTS[table], tenant=DEFAULT_TENANT_ID, **params) is not None


def test_a_version_cannot_belong_to_another_tenants_document(
    engine: Engine, other_tenant: uuid.UUID
) -> None:
    document_id = scalar(
        engine, "SELECT document_id FROM document_versions WHERE id = :id", id=new_version(engine)
    )

    with pytest.raises(IntegrityError):
        scalar(
            engine,
            "INSERT INTO document_versions (tenant_id, document_id, version_no) "
            "VALUES (:tenant, :document, 2) RETURNING id",
            tenant=other_tenant,
            document=document_id,
        )


def test_a_match_cannot_span_tenants(engine: Engine, other_tenant: uuid.UUID) -> None:
    invoice = new_version(engine, "invoice")
    order = new_version(engine, "purchase_order", other_tenant)

    with pytest.raises(IntegrityError):
        new_match(engine, invoice, order, DEFAULT_TENANT_ID)


def test_a_match_needs_an_invoice_on_one_side_and_an_order_on_the_other(engine: Engine) -> None:
    invoice, second_invoice = new_version(engine), new_version(engine)
    order = new_version(engine, "purchase_order")

    for wrong in ((invoice, second_invoice), (order, invoice), (invoice, invoice)):
        with pytest.raises(DBAPIError):
            new_match(engine, *wrong, DEFAULT_TENANT_ID)

    assert new_match(engine, invoice, order, DEFAULT_TENANT_ID) is not None


def test_the_order_number_lookup_and_the_type_filter_are_indexed(engine: Engine) -> None:
    indexes = {
        row[0]: row[1]
        for row in engine.connect().execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
        )
    }

    assert "po_no" in indexes["ix_extractions_po_no"]
    assert "doc_type" in indexes["ix_documents_tenant_type"]


def test_it_applies_to_a_database_that_already_has_records_and_can_be_undone(
    empty_database_url: URL,
) -> None:
    config = alembic_config(empty_database_url)
    command.upgrade(config, "0004")
    engine = create_engine(empty_database_url)
    invoice, order = new_version(engine), new_version(engine, "purchase_order")
    scalar(engine, INSERTS["extractions"], tenant=DEFAULT_TENANT_ID, version=invoice, sha="b" * 64)
    scalar(engine, INSERTS["assessments"], tenant=DEFAULT_TENANT_ID, version=invoice)
    new_match(engine, invoice, order, DEFAULT_TENANT_ID)

    command.upgrade(config, "head")
    command.downgrade(config, "0004")
    command.upgrade(config, "head")

    assert scalar(engine, "SELECT count(*) FROM matches") == 1
    engine.dispose()


def test_the_counterpart_lookup_as_the_service_writes_it_can_use_the_index(engine: Engine) -> None:
    """The index is on an expression, so the query must spell that expression the same way."""
    query = select(Extraction.id).where(
        Extraction.tenant_id == DEFAULT_TENANT_ID, ORDER_NUMBER == "PO-1"
    )

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        sql = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
        plan = "\n".join(row[0] for row in conn.execute(text(f"EXPLAIN {sql}")))

    assert "ix_extractions_po_no" in plan
