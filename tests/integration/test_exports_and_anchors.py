"""Exporting signed records, and anchoring the audit chain outside the database."""

import csv
import io
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from docforge import audit
from docforge.anchors import AnchorStore, verify_with_anchors
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import ReviewService
from docforge.storage import MemoryObjectStore
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
EMAIL, PIN = "asha@example.com", "482913"


@pytest.fixture
def signed(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> tuple[World, ReviewService, uuid.UUID]:
    world = World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")
    review = ReviewService(
        sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
    )
    review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", email=EMAIL, pin=PIN)
    world.process("purchase_order")
    invoice_id = world.process("invoice")
    seen = review.detail(DEFAULT_TENANT_ID, invoice_id).record_sha256
    review.sign(
        DEFAULT_TENANT_ID,
        invoice_id,
        outcome="approved",
        meaning="I approve this invoice for payment",
        reason="ok",
        override_reason=None,
        expected_record_sha256=seen,
        email=EMAIL,
        pin=PIN,
    )
    return world, review, invoice_id


def test_signed_records_export_as_rows_with_their_signature(
    signed: tuple[World, ReviewService, uuid.UUID],
) -> None:
    world, review, invoice_id = signed

    rows = review.export(DEFAULT_TENANT_ID)

    (row,) = rows
    invoice = json.loads(world.label_text)["invoice"]
    assert row["document_id"] == str(invoice_id)
    assert row["outcome"] == "approved" and row["signed_by"] == "Asha Rao"
    assert row["invoice_no"] == invoice["invoice_no"]
    assert row["grand_total"] == invoice["totals"]["grand_total"]
    assert row["supplier_gstin"] == invoice["seller"]["gstin"]
    assert len(row["record_sha256"]) == 64


def test_csv_cells_cannot_start_a_formula_in_a_spreadsheet() -> None:
    from docforge.csvsafe import to_csv

    rows = [
        {
            "filename": '=HYPERLINK("http://evil")',
            "note": "+1",
            "ok": "plain",
            "n": "-3",
            "at": "@x",
        }
    ]

    parsed = list(csv.DictReader(io.StringIO(to_csv(rows))))

    assert parsed[0]["filename"].startswith("'=")
    assert parsed[0]["note"] == "'+1" and parsed[0]["at"] == "'@x" and parsed[0]["ok"] == "plain"


def test_another_tenant_exports_nothing(
    signed: tuple[World, ReviewService, uuid.UUID], other_tenant: uuid.UUID
) -> None:
    _, review, _ = signed

    assert review.export(other_tenant) == []


def test_an_anchor_records_the_chain_head_outside_the_database(
    signed: tuple[World, ReviewService, uuid.UUID], sessions: SessionFactory
) -> None:
    store = MemoryObjectStore()
    anchors = AnchorStore(store)

    with sessions() as session:
        anchor = anchors.anchor(session, DEFAULT_TENANT_ID)
        last = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc())).first()

    assert last is not None
    assert (anchor["last_id"], anchor["last_hash"]) == (last.id, last.hash)
    assert anchors.latest(DEFAULT_TENANT_ID) == anchor
    with sessions() as session:
        report = verify_with_anchors(session, DEFAULT_TENANT_ID, anchors)
    assert report.consistent and report.anchors_checked == 1


def test_a_rewritten_log_that_still_chains_is_caught_by_its_anchor(
    signed: tuple[World, ReviewService, uuid.UUID], sessions: SessionFactory, owner_engine: Engine
) -> None:
    """An owner can rewrite the last entry and recompute its hash: the chain still verifies,
    but it no longer ends where the anchor saw it end."""
    anchors = AnchorStore(MemoryObjectStore())
    with sessions() as session:
        anchors.anchor(session, DEFAULT_TENANT_ID)
        last = session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
    assert last is not None
    forged = audit.entry_hash(
        prev_hash=last.prev_hash,
        tenant_id=last.tenant_id,
        occurred_at=last.occurred_at,
        actor="forger",
        action=last.action,
        target_type=last.target_type,
        target_id=last.target_id,
        details=last.details,
    )
    with owner_engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        conn.execute(
            text("UPDATE audit_log SET actor = 'forger', hash = :h WHERE id = :i"),
            {"h": forged, "i": last.id},
        )
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))

    with sessions() as session:
        chain = audit.verify_chain(session, DEFAULT_TENANT_ID)
        report = verify_with_anchors(session, DEFAULT_TENANT_ID, anchors)
    assert chain.consistent  # the chain alone cannot tell
    assert not report.consistent
    assert report.first_bad_id == last.id and "anchor" in (report.reason or "")


def test_the_export_endpoints_serve_csv_and_json(
    signed: tuple[World, ReviewService, uuid.UUID],
) -> None:
    from fastapi.testclient import TestClient

    from docforge.api.app import create_app
    from fakes import signed_in

    world, review, invoice_id = signed
    client = TestClient(signed_in(create_app(None, review=review), role="integrator"))

    as_csv = client.get("/v1/exports/documents.csv")
    as_json = client.get("/v1/exports/documents.json")

    assert as_csv.status_code == 200 and as_csv.headers["content-type"].startswith("text/csv")
    (row,) = list(csv.DictReader(io.StringIO(as_csv.text)))
    assert row["document_id"] == str(invoice_id) and row["outcome"] == "approved"
    (record,) = as_json.json()
    assert record["draft"]["type"] == "payment_approval_draft"
    assert record["record"]["invoice_no"]["raw"] == world.invoice_raw["invoice_no"]["text"]


def test_the_anchor_command_anchors_every_organisation(
    signed: tuple[World, ReviewService, uuid.UUID],
    sessions: SessionFactory,
    other_tenant: uuid.UUID,
) -> None:
    from docforge.anchors import anchor_all

    anchors = AnchorStore(MemoryObjectStore())

    assert anchor_all(sessions, anchors) == 2
    assert anchors.latest(DEFAULT_TENANT_ID)["last_id"] is not None  # type: ignore[index]
    assert anchors.latest(other_tenant)["last_id"] is None  # type: ignore[index]
