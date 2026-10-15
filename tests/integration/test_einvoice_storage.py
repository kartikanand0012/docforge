"""V2 1.1 part 3: an invoice's QR checked as it is read, stored with its version, and its rules
kept through review."""

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, EinvoiceCheck
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import ReviewService
from einvoice_fakes import Scanner, altered, qr_for, scanned
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
EMAIL, PIN = "asha@example.com", "482913"


@pytest.fixture
def world(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> World:
    return World(sessions, raw_invoice_from_label, raw_order_from_label, "pair_001")


def checks(sessions: SessionFactory) -> list[EinvoiceCheck]:
    with sessions() as session:
        return list(session.scalars(select(EinvoiceCheck)))


def einvoice_rules(assessment: Any) -> dict[str, tuple[str, str]]:
    """The e-invoice rules of a stored assessment (the worker's) or a reassessment (review's)."""
    data = assessment.record.data if hasattr(assessment, "record") else assessment.model_dump()
    return {
        r["rule_id"]: (r["outcome"], r["severity"])
        for r in data["rules"]
        if r["rule_id"].startswith("einvoice.")
    }


def test_a_verified_qr_is_checked_as_the_invoice_is_read_and_stored(
    world: World, sessions: SessionFactory
) -> None:
    qr = qr_for(world)
    document_id = scanned(world, Scanner(qr)).process("invoice")

    assessment = world.assessment(document_id)
    assert einvoice_rules(assessment)["einvoice.signature"] == ("passed", "error")
    assert assessment.record.decision == "accept"  # its own checks; no order to match yet
    (check,) = checks(sessions)
    assert (check.status, check.qr_text, check.page, check.pages_scanned) == ("verified", qr, 1, 1)
    assert check.decoder == "zxing-cpp test"
    assert check.evidence is not None and check.evidence["irp"] == "TEST"
    assert "einvoice.checked" in world.actions(document_id)


def test_the_audit_entry_holds_the_status_and_irp_only(
    world: World, sessions: SessionFactory
) -> None:
    scanned(world, Scanner(qr_for(world))).process("invoice")

    with sessions() as session:
        entry = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "einvoice.checked")
        ).one()
    assert entry.details == {"version_no": 1, "status": "verified", "irp": "TEST"}


def test_an_altered_qr_sends_the_invoice_to_review_and_a_correction_does_not_lift_it(
    world: World, sessions: SessionFactory
) -> None:
    document_id = scanned(world, Scanner(altered(qr_for(world)))).process("invoice")
    review = ReviewService(
        sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
    )
    review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", email=EMAIL, pin=PIN)

    assert world.assessment(document_id).record.decision == "review"
    before = review.detail(DEFAULT_TENANT_ID, document_id).assessment
    invoice_no = json.loads(world.label_text)["invoice"]["invoice_no"]
    review.correct(
        DEFAULT_TENANT_ID, document_id, path="invoice_no", text=invoice_no,
        reason="checked against the paper copy", email=EMAIL, pin=PIN,
    )  # fmt: skip
    after = review.detail(DEFAULT_TENANT_ID, document_id).assessment

    for assessment in (before, after):
        assert einvoice_rules(assessment)["einvoice.signature"] == ("failed", "error")
        assert assessment.decision == "review"


def test_a_copied_qr_is_caught_on_the_fields_that_disagree(world: World) -> None:
    document_id = scanned(world, Scanner(qr_for(world, DocNo="OTHER/1"))).process("invoice")

    assert einvoice_rules(world.assessment(document_id))["einvoice.invoice_no"] == (
        "failed", "error"
    )  # fmt: skip


def test_a_scan_that_fails_is_stored_as_unreadable_and_the_reading_goes_on(
    world: World, sessions: SessionFactory
) -> None:
    scanner = Scanner(error=RuntimeError("decoder crashed"))
    document_id = scanned(world, scanner).process("invoice")

    assessment = world.assessment(document_id)
    assert einvoice_rules(assessment) == {}
    assert assessment.record.decision == "accept"  # its own checks; no order to match yet
    (check,) = checks(sessions)
    assert (check.status, check.evidence, check.qr_text) == ("unreadable", None, None)


def test_an_invoice_without_a_qr_is_stored_as_none_with_no_rules(
    world: World, sessions: SessionFactory
) -> None:
    document_id = scanned(world, Scanner()).process("invoice")

    assert einvoice_rules(world.assessment(document_id)) == {}
    assert [check.status for check in checks(sessions)] == ["none"]


def test_only_invoices_are_scanned(world: World, sessions: SessionFactory) -> None:
    scanner = Scanner(qr_for(world))
    scanned(world, scanner).process("purchase_order")

    assert scanner.calls == 0
    assert checks(sessions) == []


def test_with_the_check_switched_off_nothing_is_scanned_or_stored(
    world: World, sessions: SessionFactory
) -> None:
    document_id = world.process("invoice")  # no checker: EINVOICE_ENABLED=false

    assert einvoice_rules(world.assessment(document_id)) == {}
    assert checks(sessions) == []


def test_a_stored_check_cannot_be_changed_or_deleted(world: World, owner_engine: Engine) -> None:
    scanned(world, Scanner(qr_for(world))).process("invoice")

    for statement in (
        "UPDATE einvoice_checks SET status = 'none'",
        "DELETE FROM einvoice_checks",
        "TRUNCATE einvoice_checks",
    ):
        with owner_engine.connect() as conn, pytest.raises(Exception, match="append-only"):
            conn.execute(text(statement))


def test_a_version_has_at_most_one_check(world: World, owner_engine: Engine) -> None:
    scanned(world, Scanner(qr_for(world))).process("invoice")

    with owner_engine.connect() as conn, pytest.raises(Exception, match=r"unique|duplicate"):
        conn.execute(
            text(
                "INSERT INTO einvoice_checks (tenant_id, document_version_id, status, "
                "pages_scanned) SELECT tenant_id, document_version_id, 'none', 0 "
                "FROM einvoice_checks"
            )
        )


def test_a_check_must_belong_to_its_versions_tenant(
    world: World, owner_engine: Engine, other_tenant: uuid.UUID
) -> None:
    scanned(world, Scanner()).process("purchase_order")  # a version with no check yet

    with owner_engine.connect() as conn, pytest.raises(Exception, match="foreign key"):
        conn.execute(
            text(
                "INSERT INTO einvoice_checks (tenant_id, document_version_id, status, "
                "pages_scanned) SELECT :other, id, 'none', 0 FROM document_versions"
            ),
            {"other": other_tenant},
        )


def test_an_account_erasure_deletes_its_checks_before_its_versions(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        definition = conn.execute(
            text("SELECT pg_get_functiondef('docforge_delete_account'::regproc)")
        ).scalar_one()

    checks_at = definition.index("DELETE FROM einvoice_checks WHERE tenant_id = t;")
    assert checks_at < definition.index("DELETE FROM document_versions WHERE tenant_id = t;")


def test_reprocessing_checks_the_new_version_and_keeps_the_old_check(
    world: World, sessions: SessionFactory
) -> None:
    document_id = scanned(world, Scanner(qr_for(world))).process("invoice")
    assert world.service is not None
    again = world.service.reprocess(
        tenant_id=DEFAULT_TENANT_ID, document_id=document_id, actor="api:reprocess"
    )
    assert world.service.process(again.id) == "succeeded"

    stored = checks(sessions)
    assert len(stored) == 2
    assert len({check.document_version_id for check in stored}) == 2


def test_a_stored_check_that_cannot_be_read_keeps_the_invoice_in_review(
    world: World, sessions: SessionFactory, owner_engine: Engine
) -> None:
    # A check stored by an older release whose evidence no longer fits: review neither fails
    # nor silently drops the e-invoice rules.
    document_id = world.process("invoice")  # read with the check switched off
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO einvoice_checks (tenant_id, document_version_id, status, evidence, "
                "pages_scanned) SELECT tenant_id, id, 'verified', '{\"old\": true}', 1 "
                "FROM document_versions"
            )
        )
    review = ReviewService(
        sessions, world.store, {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC}
    )

    assessment = review.detail(DEFAULT_TENANT_ID, document_id).assessment

    assert einvoice_rules(assessment) == {"einvoice.signature": ("not_evaluated", "error")}
    assert assessment.decision == "review"
