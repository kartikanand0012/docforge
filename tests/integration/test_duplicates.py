"""V2 3.3: a bill received twice - another scan, a photo, a reprint - is flagged on the later
invoice, by the same IRN or the same seller GSTIN and invoice number, as the readings have them
after corrections."""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import literal, select, text
from sqlalchemy.engine import Engine

from docforge.api.app import create_app
from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import (
    CORRECTED_KEY,
    INVOICE_NUMBER_KEY,
    SIGNED_IRN,
    Correction,
    EinvoiceCheck,
    Extraction,
)
from docforge.db.session import SessionFactory
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.review.service import ApprovalBlocked, ReviewService
from docforge.trust import duplicates
from einvoice_fakes import Scanner, qr_for, scanned
from fakes import signed_in
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
REPO = Path(__file__).resolve().parents[2]
EMAIL, PIN = "asha@example.com", "482913"
NUMBER = "NVM/26-27/32001"  # pair_001's invoice number
OTHER_SELLER = "27AAKCS4821M1ZW"  # pair_002's seller
SAME_SELLER_AND_NUMBER = "which has the same seller GSTIN and invoice number"


class Bills:
    """Copies of pair_001's invoice: each a different file, as a second scan would be."""

    def __init__(
        self, sessions: SessionFactory, invoice: RawFromLabel, order: RawFromLabel
    ) -> None:
        self.sessions, self.invoice, self.order = sessions, invoice, order
        self.review = ReviewService(
            sessions, World(sessions, invoice, order, "pair_001").store,
            {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        )  # fmt: skip
        self.reviewer_id = self.review.add_reviewer(
            DEFAULT_TENANT_ID, name="Asha Rao", email=EMAIL, pin=PIN
        )
        self.copies = 0

    def world(self, scanner: Scanner | None = None, **printed: str) -> World:
        """A copy whose page prints `printed` (path -> text) where it differs."""
        self.copies += 1
        world = World(self.sessions, self.invoice, self.order, "pair_001")
        world.invoice_pdf += f"\n%copy {self.copies}\n".encode()
        for path, shown in printed.items():
            world.reprint_invoice(path.replace("__", "."), shown)
        if scanner is not None:
            scanned(world, scanner)
        world.build()
        self.review = ReviewService(
            self.sessions, world.store,
            {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        )  # fmt: skip
        return world

    def receive(self, filename: str, scanner: Scanner | None = None, **printed: str) -> uuid.UUID:
        world = self.world(scanner, **printed)
        return world.process_pdf("invoice", world.invoice_pdf, filename)

    def duplicates(self, document_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
        detail = self.review.detail(DEFAULT_TENANT_ID, document_id)
        return [(d.document_id, d.filename, d.matched_on) for d in detail.duplicates]

    def blockers(self, document_id: uuid.UUID) -> tuple[str, ...]:
        return self.review.detail(DEFAULT_TENANT_ID, document_id).blockers

    def correct(self, document_id: uuid.UUID, path: str, text: str) -> None:
        self.review.correct(
            DEFAULT_TENANT_ID, document_id, path=path, text=text, reason="the paper copy",
            email=EMAIL, pin=PIN,
        )  # fmt: skip


@pytest.fixture
def bills(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Bills:
    return Bills(sessions, raw_invoice_from_label, raw_order_from_label)


def test_a_second_copy_of_a_bill_is_flagged_as_a_duplicate_of_the_first(bills: Bills) -> None:
    first = bills.receive("bill.pdf")
    second = bills.receive("bill-scan.pdf")

    assert bills.duplicates(second) == [(first, "bill.pdf", "seller_and_number")]
    assert f"it may be a duplicate of bill.pdf, {SAME_SELLER_AND_NUMBER}" in bills.blockers(second)
    # The first one received is the original.
    assert bills.duplicates(first) == []
    assert not any("duplicate" in blocker for blocker in bills.blockers(first))


def test_the_number_is_compared_without_case_or_spaces(bills: Bills) -> None:
    first = bills.receive("bill.pdf")
    second = bills.receive("photo.pdf", invoice_no="nvm/26-27/ 32001")

    assert bills.duplicates(second) == [(first, "bill.pdf", "seller_and_number")]


def test_another_number_or_another_seller_is_another_bill(bills: Bills) -> None:
    bills.receive("bill.pdf")
    other_number = bills.receive("next.pdf", invoice_no="NVM/26-27/32002")
    other_seller = bills.receive("theirs.pdf", seller__gstin=OTHER_SELLER)

    assert bills.duplicates(other_number) == []
    assert bills.duplicates(other_seller) == []


def test_a_correction_on_the_later_bill_can_make_it_a_duplicate(bills: Bills) -> None:
    first = bills.receive("bill.pdf")
    second = bills.receive("photo.pdf", invoice_no="NVM/26-27/32007")  # misread
    assert bills.duplicates(second) == []

    bills.correct(second, "invoice_no", NUMBER)

    assert bills.duplicates(second) == [(first, "bill.pdf", "seller_and_number")]


def test_a_correction_on_the_earlier_bill_is_seen_too(bills: Bills) -> None:
    first = bills.receive("bill.pdf", invoice_no="NVM/26-27/32007")  # misread
    second = bills.receive("photo.pdf")
    assert bills.duplicates(second) == []

    bills.correct(first, "invoice_no", NUMBER)
    assert bills.duplicates(second) == [(first, "bill.pdf", "seller_and_number")]

    bills.correct(first, "invoice_no", "NVM/26-27/32008")  # and corrected away again
    assert bills.duplicates(second) == []


def test_a_bill_sharing_the_irn_is_a_duplicate_whatever_its_page_says(bills: Bills) -> None:
    template = bills.world()  # the QR as pair_001's portal signed it
    qr = qr_for(template)
    first = bills.receive("bill.pdf", Scanner(qr))
    second = bills.receive("reprint.pdf", Scanner(qr), invoice_no="NVM/26-27/32009")

    assert bills.duplicates(second) == [(first, "bill.pdf", "irn")]
    assert "it may be a duplicate of bill.pdf, which has the same IRN" in bills.blockers(second)


def test_an_earlier_bill_deleted_or_rejected_does_not_count(bills: Bills) -> None:
    deleted = bills.receive("deleted.pdf")
    world = bills.world()
    assert world.service is not None
    world.service.delete(DEFAULT_TENANT_ID, deleted, actor="api:delete")
    rejected = bills.receive("rejected.pdf")
    detail = bills.review.detail(DEFAULT_TENANT_ID, rejected)
    bills.review.sign(
        DEFAULT_TENANT_ID, rejected, outcome="rejected", meaning="I reject this invoice",
        reason="a wrong rate", override_reason=None,
        expected_record_sha256=detail.record_sha256, email=EMAIL, pin=PIN,
    )  # fmt: skip

    resent = bills.receive("resent.pdf")

    assert bills.duplicates(resent) == []


def test_another_workspaces_bill_is_never_seen(bills: Bills, other_tenant: uuid.UUID) -> None:
    world = bills.world()
    assert world.service is not None
    theirs = world.service.ingest(
        tenant_id=other_tenant, doc_type="invoice", filename="theirs.pdf",
        data=world.invoice_pdf, actor="api:upload",
    )  # fmt: skip
    assert theirs.version is not None
    assert world.service.process(theirs.version.id) == "succeeded"

    mine = bills.receive("mine.pdf")

    assert bills.duplicates(mine) == []


def test_reading_a_bill_again_does_not_make_it_its_own_duplicate(bills: Bills) -> None:
    world = bills.world()
    first = world.process_pdf("invoice", world.invoice_pdf, "bill.pdf")
    assert world.service is not None
    again = world.service.reprocess(
        tenant_id=DEFAULT_TENANT_ID, document_id=first, actor="api:reprocess"
    )
    assert world.service.process(again.id) == "succeeded"

    assert bills.duplicates(first) == []


def test_a_duplicate_waits_in_the_queue_and_needs_an_override_to_approve(bills: Bills) -> None:
    bills.receive("bill.pdf")
    second = bills.receive("bill-scan.pdf")

    (item,) = [i for i in bills.review.queue(DEFAULT_TENANT_ID) if i.document_id == second]
    assert any("duplicate of bill.pdf" in reason for reason in item.reasons)
    detail = bills.review.detail(DEFAULT_TENANT_ID, second)
    with pytest.raises(ApprovalBlocked):
        bills.review.sign(
            DEFAULT_TENANT_ID, second, outcome="approved",
            meaning="I approve this invoice for payment", reason="checked", override_reason=None,
            expected_record_sha256=detail.record_sha256, email=EMAIL, pin=PIN,
        )  # fmt: skip


def test_the_review_response_names_the_earlier_bill(bills: Bills) -> None:
    first = bills.receive("bill.pdf")
    second = bills.receive("bill-scan.pdf")
    app = create_app(
        None, review=bills.review, evals_dir=REPO / "evals" / "baselines",
        cors_origins=("http://localhost:3000",),
    )  # fmt: skip
    api = TestClient(signed_in(app, reviewer_id=bills.reviewer_id))

    body = api.get(f"/v1/documents/{second}/review").json()

    assert body["duplicates"] == [
        {"document_id": str(first), "filename": "bill.pdf", "matched_on": "seller_and_number"}
    ]
    schema = api.get("/openapi.json").json()["components"]["schemas"]
    assert set(schema["DuplicateOut"]["properties"]["matched_on"]["enum"]) == {
        "irn", "seller_and_number"
    }  # fmt: skip


def test_other_sellers_bills_with_the_number_never_hide_the_duplicate(
    bills: Bills, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(duplicates, "_CANDIDATES", 2)
    for n in range(3):  # more than the cap, each another seller's bill with the same number
        bills.receive(f"theirs-{n}.pdf", seller__gstin=OTHER_SELLER)
    first = bills.receive("bill.pdf")
    second = bills.receive("bill-scan.pdf")

    assert bills.duplicates(second) == [(first, "bill.pdf", "seller_and_number")]


def test_unusual_spaces_in_the_number_are_spaces_too(bills: Bills) -> None:
    first = bills.receive("bill.pdf")
    second = bills.receive("photo.pdf", invoice_no="NVM/26-27/" + "\u00a0" + "32001")

    assert bills.duplicates(second) == [(first, "bill.pdf", "seller_and_number")]


def test_a_correction_of_the_earlier_bills_seller_is_seen(bills: Bills) -> None:
    first = bills.receive("bill.pdf", seller__gstin=OTHER_SELLER)  # misread seller
    second = bills.receive("photo.pdf")
    assert bills.duplicates(second) == []

    bills.correct(first, "seller.gstin", "24AABFN7754K1Z0")

    assert bills.duplicates(second) == [(first, "bill.pdf", "seller_and_number")]


def test_rejecting_the_original_later_clears_the_flag(bills: Bills) -> None:
    first = bills.receive("bill.pdf")
    second = bills.receive("bill-scan.pdf")
    assert bills.duplicates(second) != []
    detail = bills.review.detail(DEFAULT_TENANT_ID, first)
    bills.review.sign(
        DEFAULT_TENANT_ID, first, outcome="rejected", meaning="I reject this invoice",
        reason="a wrong rate", override_reason=None,
        expected_record_sha256=detail.record_sha256, email=EMAIL, pin=PIN,
    )  # fmt: skip

    assert bills.duplicates(second) == []


@pytest.mark.parametrize(
    ("query", "index"),
    [
        (
            select(Extraction.document_version_id).where(
                Extraction.tenant_id == DEFAULT_TENANT_ID, literal("x/1") == INVOICE_NUMBER_KEY
            ),
            "ix_extractions_invoice_no_key",
        ),
        (
            select(Correction.document_version_id).where(
                Correction.tenant_id == DEFAULT_TENANT_ID,
                Correction.path == "invoice_no",
                literal("x/1") == CORRECTED_KEY,
            ),
            "ix_corrections_invoice_no_key",
        ),
        (
            select(EinvoiceCheck.document_version_id).where(
                EinvoiceCheck.tenant_id == DEFAULT_TENANT_ID,
                EinvoiceCheck.status == "verified",
                literal("b" * 64) == SIGNED_IRN,
            ),
            "ix_einvoice_checks_irn",
        ),
    ],
)
def test_each_lookup_is_spelled_as_its_index(query: Any, index: str, owner_engine: Engine) -> None:
    # Spelled differently from its index, a lookup would read the whole table.
    with owner_engine.connect() as conn:
        conn.execute(text("SET enable_seqscan = off"))
        plan = (
            conn.execute(
                text(
                    "EXPLAIN "
                    + str(
                        query.compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True})
                    )
                )
            )
            .scalars()
            .all()
        )
    assert index in "\n".join(plan)
