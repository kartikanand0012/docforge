"""Assessments and invoice-to-order matches stored with each version and served by the API."""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from docforge import audit
from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AssessmentRecord, AuditEntry, DocumentVersion, MatchRecord
from docforge.db.session import SessionFactory
from fakes import reprint, signed_in
from worlds import World

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    return World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")


def test_a_correct_invoice_is_stored_as_accepted(world: World) -> None:
    document_id = world.process("invoice")

    detail = world.assessment(document_id)

    assert detail.record.decision == "accept"
    assert detail.record.data["reasons"] == []
    assert detail.match is None
    # Correct on its own, but nothing independent corroborates it yet.
    assert (detail.decision, detail.match_status) == ("review", "no_counterpart")
    assert world.actions(document_id)[-2:] == ["extraction.created", "assessment.created"]


def test_a_wrong_batch_number_is_stored_as_needing_review(world: World) -> None:
    world.invoice_raw["lines"][0]["batch_no"]["text"] = "XGX944O68"

    detail = world.assessment(world.process("invoice"))

    assert detail.decision == "review"
    flagged = [f["path"] for f in detail.record.data["fields"] if f["needs_review"]]
    assert flagged == ["lines[0].batch_no"]


def test_the_audit_entry_summarises_the_assessment(world: World, sessions: SessionFactory) -> None:
    world.reprint_invoice("lines[0].amount", "1316.36")
    world.process("invoice")

    with sessions() as session:
        entry = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "assessment.created")
        ).one()
        assert audit.verify_chain(session, DEFAULT_TENANT_ID).consistent
    assert entry.details == {
        "version_no": 1,
        "decision": "review",
        "values_flagged": 3,  # the line amount and the two tax totals it no longer adds up to
        "checks_failed": 2,
    }


def test_there_is_no_assessment_before_extraction(world: World) -> None:
    service = world.build()
    result = service.ingest(
        tenant_id=DEFAULT_TENANT_ID,
        doc_type="invoice",
        filename="i.pdf",
        data=world.invoice_pdf,
        actor="api:upload",
    )

    assert service.assessment(DEFAULT_TENANT_ID, result.document.id) is None


@pytest.mark.parametrize("first", ["invoice", "purchase_order"])
def test_an_invoice_and_its_order_are_matched_whichever_arrives_first(
    world: World, first: str
) -> None:
    second = "purchase_order" if first == "invoice" else "invoice"
    first_id, second_id = world.process(first), world.process(second)
    ids = {first: first_id, second: second_id}

    invoice, order = world.assessment(ids["invoice"]), world.assessment(ids["purchase_order"])

    assert invoice.match is not None
    assert order.match is not None
    assert invoice.match.id == order.match.id
    assert (invoice.match.decision, invoice.match.data["discrepancies"]) == ("match", [])
    assert invoice.counterpart_document_id == ids["purchase_order"]
    assert order.counterpart_document_id == ids["invoice"]
    assert invoice.decision == "accept"
    assert world.actions(ids["invoice"]).count("match.created") == 1
    assert world.actions(ids["purchase_order"]).count("match.created") == 1


def test_a_quantity_that_differs_from_the_order_is_a_mismatch(world: World) -> None:
    # The invoice itself is consistent (20 x 59.51 less 2% became 25 x ...), so only the
    # comparison with the order can catch it.
    world.reprint_invoice("lines[0].qty", "25")
    world.reprint_invoice("lines[0].taxable_value", "1458.00")
    world.reprint_invoice("lines[0].amount", "1632.96")
    world.reprint_invoice("totals.taxable_value", "90969.53")
    world.reprint_invoice("totals.cgst", "4027.15")
    world.reprint_invoice("totals.sgst", "4027.15")
    world.reprint_invoice("totals.round_off", "0.17")
    world.reprint_invoice("totals.grand_total", "99024.00")
    world.process("purchase_order")

    detail = world.assessment(world.process("invoice"))

    assert detail.record.decision == "accept"  # nothing wrong with the invoice on its own
    assert detail.match.decision == "mismatch"
    assert [d["code"] for d in detail.match.data["discrepancies"]] == ["line.qty"]
    assert detail.decision == "review"


def test_an_invoice_with_no_order_on_file_has_no_match(world: World) -> None:
    assert world.assessment(world.process("invoice")).match is None


def test_an_order_on_its_own_is_accepted(world: World) -> None:
    detail = world.assessment(world.process("purchase_order"))

    assert (detail.decision, detail.match_status) == ("accept", "no_counterpart")


def test_an_order_from_another_supplier_with_the_same_number_is_not_the_counterpart(
    world: World,
) -> None:
    world.order_parsed = reprint(
        world.order_parsed, world.order_raw, "supplier_gstin", "27AAPFU0939F1ZV"
    )
    world.process("purchase_order")

    detail = world.assessment(world.process("invoice"))

    assert detail.match is None
    assert detail.match_status == "no_counterpart"


def test_reprocessing_an_invoice_matches_its_new_version_too(world: World) -> None:
    world.process("purchase_order")
    document_id = world.process("invoice")
    assert world.service is not None
    version = world.service.reprocess(
        tenant_id=DEFAULT_TENANT_ID, document_id=document_id, actor="api:x"
    )
    world.service.process(version.id)

    with world.sessions() as session:
        matches = list(session.scalars(select(MatchRecord)))
        assessments = list(session.scalars(select(AssessmentRecord)))
    assert len(matches) == 2
    assert len(assessments) == 3  # order, invoice v1, invoice v2
    assert world.assessment(document_id).match.invoice_version_id == version.id


def test_a_match_with_an_order_version_that_was_replaced_no_longer_counts(world: World) -> None:
    order_id = world.process("purchase_order")
    invoice_id = world.process("invoice")
    assert world.assessment(invoice_id).match_status == "match"

    # The order is read again and now names another supplier, so it is no longer this
    # invoice's order. The comparison with the old reading must not keep the invoice accepted.
    world.order_parsed = reprint(
        world.order_parsed, world.order_raw, "supplier_gstin", "27AAPFU0939F1ZV"
    )
    service = world.build()
    version = service.reprocess(tenant_id=DEFAULT_TENANT_ID, document_id=order_id, actor="api:x")
    assert service.process(version.id) == "succeeded"

    detail = world.assessment(invoice_id)
    assert (detail.decision, detail.match_status) == ("review", "no_counterpart")
    assert detail.counterpart_document_id is None


@pytest.mark.parametrize("table", ["assessments", "matches"])
def test_stored_assessments_and_matches_cannot_be_changed(
    world: World, owner_engine: Engine, table: str
) -> None:
    world.process("purchase_order")
    world.process("invoice")

    for statement in (f"UPDATE {table} SET decision = 'accept'", f"DELETE FROM {table}"):  # noqa: S608
        with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as conn:
            conn.execute(text(statement))


def test_the_api_serves_the_assessment_with_boxes_and_the_match(world: World) -> None:
    world.invoice_raw["lines"][0]["batch_no"]["text"] = "XGX944O68"
    world.process("purchase_order")
    document_id = world.process("invoice")
    assert world.service is not None
    client = TestClient(signed_in(create_app(None, service=world.service)))

    response = client.get(f"/v1/documents/{document_id}/assessment")

    assert response.status_code == 200
    body = response.json()
    assert (body["decision"], body["version_no"]) == ("review", 1)
    assert body["assessment"]["decision"] == "review"
    flagged = [f for f in body["assessment"]["fields"] if f["needs_review"]]
    assert [f["path"] for f in flagged] == ["lines[0].batch_no"]
    assert set(flagged[0]["boxes"][0]) == {"page", "x0", "y0", "x1", "y1"}
    assert body["match"]["decision"] == "match"
    assert body["match"]["counterpart_document_id"] is not None


def test_the_api_reports_no_assessment_yet(world: World) -> None:
    service = world.build()
    result = service.ingest(
        tenant_id=DEFAULT_TENANT_ID,
        doc_type="invoice",
        filename="i.pdf",
        data=world.invoice_pdf,
        actor="api:upload",
    )
    client = TestClient(signed_in(create_app(None, service=service)))

    response = client.get(f"/v1/documents/{result.document.id}/assessment")

    assert response.status_code == 404
    assert client.get(f"/v1/documents/{uuid.uuid4()}/assessment").status_code == 404


def test_versions_reference(sessions: SessionFactory, world: World) -> None:
    document_id = world.process("invoice")

    with sessions() as session:
        record = session.scalars(select(AssessmentRecord)).one()
        version = session.get_one(DocumentVersion, record.document_version_id)
    assert version.document_id == document_id
