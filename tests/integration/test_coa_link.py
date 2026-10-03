"""An invoice is checked against the certificates of analysis for the batches it bills."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.session import SessionFactory
from docforge.extraction.coa import COA_SPEC
from docforge.extraction.pipeline import INVOICE_SPEC, DocumentSpec
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import ReviewService
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
SPECS: dict[str, DocumentSpec[Any]] = {
    "invoice": INVOICE_SPEC,
    "purchase_order": PURCHASE_ORDER_SPEC,
    "coa": COA_SPEC,
}


def setup(
    sessions: SessionFactory,
    pair: str,
    case: str,
    builders: tuple[RawFromLabel, RawFromLabel, RawFromLabel],
) -> tuple[World, ReviewService, uuid.UUID, uuid.UUID]:
    invoice_build, order_build, coa_build = builders
    world = World(sessions, invoice_build, order_build, pair)
    coa_pdf = world.add_coa(case, coa_build)
    review = ReviewService(sessions, world.store, SPECS)
    world.process("purchase_order")
    invoice_id = world.process("invoice")
    coa_id = world.process_pdf("coa", coa_pdf, "coa.pdf")
    return world, review, invoice_id, coa_id


def test_a_clean_certificate_for_a_billed_batch_is_linked_and_blocks_nothing(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    raw_coa_from_label: RawFromLabel,
) -> None:
    world, review, invoice_id, coa_id = setup(
        sessions,
        "pair_001",
        "coa_001",
        (raw_invoice_from_label, raw_order_from_label, raw_coa_from_label),
    )

    detail = review.detail(DEFAULT_TENANT_ID, invoice_id)

    batch = world.invoice_raw["lines"][0]["batch_no"]["text"]
    assert [(c["batch_no"], c["status"], c["document_id"]) for c in detail.certificates] == [
        (batch, "within_limits", str(coa_id))
    ]
    assert detail.blockers == ()
    assert review.detail(DEFAULT_TENANT_ID, coa_id).decision == "accept"


def test_a_certificate_with_a_result_out_of_limit_holds_the_invoice_back(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    raw_coa_from_label: RawFromLabel,
) -> None:
    world, review, invoice_id, coa_id = setup(
        sessions,
        "pair_002",
        "coa_002",
        (raw_invoice_from_label, raw_order_from_label, raw_coa_from_label),
    )

    invoice = review.detail(DEFAULT_TENANT_ID, invoice_id)
    coa = review.detail(DEFAULT_TENANT_ID, coa_id)

    batch = world.invoice_raw["lines"][0]["batch_no"]["text"]
    assert [(c["batch_no"], c["status"]) for c in invoice.certificates] == [(batch, "out_of_limit")]
    assert f"the certificate for batch {batch} has results outside their limits" in invoice.blockers
    assert coa.decision == "review"
    assert any("coa.result_within_limit" in reason for reason in coa.blockers)


def test_a_certificate_that_cannot_be_checked_holds_the_invoice_back_as_unverified(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    raw_coa_from_label: RawFromLabel,
) -> None:
    def unreadable(label: dict[str, Any]) -> dict[str, Any]:
        raw = raw_coa_from_label(label)
        raw["tests"][3]["result"] = {"text": "see attached", "block_ids": []}
        return raw

    world, review, invoice_id, _ = setup(
        sessions, "pair_001", "coa_001", (raw_invoice_from_label, raw_order_from_label, unreadable)
    )

    detail = review.detail(DEFAULT_TENANT_ID, invoice_id)

    batch = world.invoice_raw["lines"][0]["batch_no"]["text"]
    assert [c["status"] for c in detail.certificates] == ["unverified"]
    assert f"the certificate for batch {batch} could not be fully checked" in detail.blockers


def test_a_batch_is_matched_whatever_its_case_or_spacing(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
    raw_coa_from_label: RawFromLabel,
) -> None:
    def lower(label: dict[str, Any]) -> dict[str, Any]:
        raw = raw_coa_from_label(label)
        raw["batch_no"]["text"] = f" {raw['batch_no']['text'].lower()} "
        return raw

    _, review, invoice_id, _ = setup(
        sessions, "pair_002", "coa_002", (raw_invoice_from_label, raw_order_from_label, lower)
    )

    assert [c["status"] for c in review.detail(DEFAULT_TENANT_ID, invoice_id).certificates] == [
        "out_of_limit"
    ]
