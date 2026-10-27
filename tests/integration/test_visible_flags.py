"""V2 P0: the checks already built - duplicates, near-expiry stock, Schedule H1 - made visible.

Each queue row counts its flags, each bill lists its near-expiry lines with the months they
have left, and a workspace can count what was caught over a range of days. Two QA bugs of
2026-10-09 are proved here too: the place of supply's code after a correction, and an invoice
and its order linked whichever is read first - also after a reviewer corrects the order
number, or by hand when nothing on the page links them.
"""

import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from docforge.db import DEFAULT_TENANT_ID
from docforge.db.models import AuditEntry, MatchRecord
from docforge.db.session import SessionFactory
from docforge.documents import DocumentNotFound
from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.h1_products import H1ProductService
from docforge.review.flags import FlagCounts
from docforge.review.service import AlreadySigned, NotLinkable, ReviewService
from docforge.workspace_settings import WorkspaceSettingsService
from einvoice_fakes import Scanner, qr_for, scanned
from worlds import World

pytestmark = pytest.mark.integration

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
EMAIL, PIN = "asha@example.com", "482913"
MEANING = "I approve this invoice for payment"
NUMBER = "NVM/26-27/32001"  # pair_001's invoice number
ORDER_NO = "PO-JPH-12001"  # pair_001's order number
MISREAD_ORDER_NO = "PO-JPH-12OO1"  # letters O for zeros, as a poor scan reads it
OTHER_SELLER = "27AAKCS4821M1ZW"  # pair_002's seller
FOUR_MONTHS_LEFT = "01/27"  # pair_001 is billed on 2 September 2026
PRODUCT = "Amoxicillin Capsules IP 250mg"  # pair_001's first line


def _path(name: str) -> str:
    """`lines__0__expiry` as `lines[0].expiry`, `seller__gstin` as `seller.gstin`."""
    return re.sub(r"__(\d+)__", r"[\1].", name).replace("__", ".")


class Desk:
    """pair_001's bills and order, received as separate files, and the review service."""

    def __init__(
        self, sessions: SessionFactory, invoice: RawFromLabel, order: RawFromLabel
    ) -> None:
        self.sessions, self.invoice, self.order = sessions, invoice, order
        self.copies = 0
        self.last: World = World(sessions, invoice, order, "pair_001")
        self.review = self._review(self.last)
        self.review.add_reviewer(DEFAULT_TENANT_ID, name="Asha Rao", email=EMAIL, pin=PIN)

    def _review(self, world: World) -> ReviewService:
        return ReviewService(
            self.sessions, world.store,
            {"invoice": INVOICE_SPEC, "purchase_order": PURCHASE_ORDER_SPEC},
        )  # fmt: skip

    def world(self, scanner: Scanner | None = None, **printed: str) -> World:
        """A copy whose invoice page prints `printed` (path -> text) where it differs."""
        self.copies += 1
        world = World(self.sessions, self.invoice, self.order, "pair_001")
        world.invoice_pdf += f"\n%copy {self.copies}\n".encode()
        world.order_pdf += f"\n%copy {self.copies}\n".encode()
        for path, shown in printed.items():
            world.reprint_invoice(_path(path), shown)
        if scanner is not None:
            scanned(world, scanner)
        world.build()
        self.last = world
        self.review = self._review(world)
        return world

    def receive(self, filename: str, scanner: Scanner | None = None, **printed: str) -> uuid.UUID:
        world = self.world(scanner, **printed)
        return world.process_pdf("invoice", world.invoice_pdf, filename)

    def receive_order(self, filename: str = "order.pdf") -> uuid.UUID:
        world = self.world()
        return world.process_pdf("purchase_order", world.order_pdf, filename)

    def flags(self, document_id: uuid.UUID) -> FlagCounts:
        (item,) = [i for i in self.review.queue(DEFAULT_TENANT_ID) if i.document_id == document_id]
        return item.flags

    def correct(self, document_id: uuid.UUID, path: str, value: str) -> Any:
        return self.review.correct(
            DEFAULT_TENANT_ID, document_id, path=path, text=value, reason="the paper copy",
            email=EMAIL, pin=PIN,
        )  # fmt: skip

    def sign(self, document_id: uuid.UUID, override_reason: str | None = None) -> Any:
        detail = self.review.detail(DEFAULT_TENANT_ID, document_id)
        return self.review.sign(
            DEFAULT_TENANT_ID, document_id, outcome="approved", meaning=MEANING,
            reason="checked", override_reason=override_reason,
            expected_record_sha256=detail.record_sha256, email=EMAIL, pin=PIN,
        )  # fmt: skip

    def link(self, invoice_id: uuid.UUID, order_id: uuid.UUID, reason: str = "same order") -> Any:
        return self.review.link_order(
            DEFAULT_TENANT_ID, invoice_id, order_id, reason=reason,
            actor="user:asha", name="Asha Rao",
        )  # fmt: skip

    def mark_h1(self, product: str = PRODUCT) -> None:
        H1ProductService(self.sessions).mark(
            DEFAULT_TENANT_ID, product, actor="user:asha", name="Asha Rao"
        )

    def window(self, months: int) -> None:
        WorkspaceSettingsService(self.sessions).update(
            DEFAULT_TENANT_ID, near_expiry_months=months, actor="cli", name="Asha Rao"
        )

    def actions(self) -> list[str]:
        with self.sessions() as session:
            return list(session.scalars(select(AuditEntry.action).order_by(AuditEntry.id)))


@pytest.fixture
def desk(
    sessions: SessionFactory,
    raw_invoice_from_label: RawFromLabel,
    raw_order_from_label: RawFromLabel,
) -> Desk:
    return Desk(sessions, raw_invoice_from_label, raw_order_from_label)


def _today() -> date:
    """India's date: the summary's days are India's."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


# AC-P0-1: the same verified IRN


def test_a_bill_with_the_irn_of_an_earlier_one_is_a_duplicate_in_the_queue(desk: Desk) -> None:
    qr = qr_for(desk.world())
    first = desk.receive("INV-2041.pdf", Scanner(qr))
    second = desk.receive("reprint.pdf", Scanner(qr), invoice_no="NVM/26-27/32009")

    assert desk.flags(second).duplicates == 1
    detail = desk.review.detail(DEFAULT_TENANT_ID, second)
    assert [(d.document_id, d.filename, d.matched_on) for d in detail.duplicates] == [
        (first, "INV-2041.pdf", "irn")
    ]
    with pytest.raises(Exception, match="override"):
        desk.sign(second)
    assert desk.sign(second, override_reason="credit note raised").outcome == "approved"


# AC-P0-2: the same seller GSTIN and invoice number, however spaced or cased


def test_the_same_number_in_other_case_and_spacing_is_a_duplicate(desk: Desk) -> None:
    desk.receive("INV-2041.pdf")
    second = desk.receive("scan.pdf", invoice_no="nvm / 26-27 / 32001")

    assert desk.flags(second).duplicates == 1
    detail = desk.review.detail(DEFAULT_TENANT_ID, second)
    assert [d.matched_on for d in detail.duplicates] == ["seller_and_number"]


def test_another_seller_with_the_same_number_is_not_a_duplicate(desk: Desk) -> None:
    desk.receive("INV-2041.pdf")
    second = desk.receive("other.pdf", seller__gstin=OTHER_SELLER)

    assert desk.review.detail(DEFAULT_TENANT_ID, second).duplicates == ()
    assert desk.flags(second).duplicates == 0


def test_separators_still_count_as_decided_for_duplicates(desk: Desk) -> None:
    # Owner decision (v2-3.3, kept 2026-10-27): "/" and "-" are part of the number.
    desk.receive("INV-2041.pdf")
    second = desk.receive("other.pdf", invoice_no="NVM-26-27-32001")

    assert desk.review.detail(DEFAULT_TENANT_ID, second).duplicates == ()


# AC-P0-3: near-expiry lines, with the months they have left


def test_a_line_inside_the_window_is_listed_with_its_months_left(desk: Desk) -> None:
    bill = desk.receive("bill.pdf", lines__0__expiry=FOUR_MONTHS_LEFT)

    detail = desk.review.detail(DEFAULT_TENANT_ID, bill)

    assert [(n.path, n.months_left, n.expiry) for n in detail.near_expiry] == [
        ("lines[0]", 4, "2027-01")
    ]
    (line,) = detail.near_expiry
    assert (line.product_name, line.batch_no) == (PRODUCT, "XGX944068")
    assert desk.flags(bill).near_expiry_lines == 1


def test_a_line_outside_the_window_or_a_window_of_zero_is_not_listed(desk: Desk) -> None:
    bill = desk.receive("bill.pdf", lines__0__expiry=FOUR_MONTHS_LEFT)

    desk.window(4)  # four months left is not less than four
    assert desk.review.detail(DEFAULT_TENANT_ID, bill).near_expiry == ()
    desk.window(0)
    assert desk.review.detail(DEFAULT_TENANT_ID, bill).near_expiry == ()
    desk.window(5)
    assert len(desk.review.detail(DEFAULT_TENANT_ID, bill).near_expiry) == 1


def test_a_corrected_expiry_is_the_one_flagged(desk: Desk) -> None:
    bill = desk.receive("bill.pdf")
    assert desk.review.detail(DEFAULT_TENANT_ID, bill).near_expiry == ()

    detail = desk.correct(bill, "lines[2].expiry", "12/26")

    assert [(n.path, n.months_left) for n in detail.near_expiry] == [("lines[2]", 3)]


# AC-P0-4: Schedule H1 lines counted; neither flag ever needs an override


def test_h1_and_near_expiry_are_counted_but_never_need_an_override(desk: Desk) -> None:
    desk.receive_order()
    desk.mark_h1()
    bill = desk.receive("bill.pdf", lines__0__expiry=FOUR_MONTHS_LEFT)

    detail = desk.review.detail(DEFAULT_TENANT_ID, bill)
    assert detail.match_status == "match"
    assert [line.path for line in detail.schedule_h1] == ["lines[0]"]
    assert len(detail.near_expiry) == 1
    assert detail.blockers == ()  # flags only: not in the queue, approved without a reason
    assert all(i.document_id != bill for i in desk.review.queue(DEFAULT_TENANT_ID))
    assert desk.sign(bill).override_reason is None


def test_the_queue_counts_h1_lines_on_a_bill_that_waits_for_other_reasons(desk: Desk) -> None:
    desk.mark_h1()
    bill = desk.receive("bill.pdf")  # no order on file: it waits in the queue

    assert desk.flags(bill) == FlagCounts(duplicates=0, near_expiry_lines=0, schedule_h1_lines=1)


# AC-P0-5: the place of supply's code after a reviewer's correction


@pytest.mark.parametrize(
    ("printed", "code"),
    [
        ("Gujarat (24)", "24"),
        ("Gujarat [24]", "24"),
        ("State: Gujarat, Code: 24", "24"),
        ("Gujarat (99)", None),  # not a GST state code
        ("Gujarat", None),
    ],
)
def test_a_corrected_place_of_supply_has_a_two_digit_code_or_none(
    desk: Desk, printed: str, code: str | None
) -> None:
    bill = desk.receive("bill.pdf")
    assert (
        desk.review.detail(DEFAULT_TENANT_ID, bill).record["place_of_supply_code"]["value"] == "24"
    )

    detail = desk.correct(bill, "place_of_supply", printed)

    assert detail.record["place_of_supply_code"]["value"] == code
    assert detail.record["place_of_supply"]["value"] == "Gujarat"


# AC-P0-6: an invoice and its order end up linked, whichever is read first


@pytest.mark.parametrize("first", ["invoice", "purchase_order"])
def test_a_corrected_order_number_links_the_invoice_to_its_order(desk: Desk, first: str) -> None:
    if first == "invoice":
        bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)
        order = desk.receive_order()
    else:
        order = desk.receive_order()
        bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)
    assert desk.review.detail(DEFAULT_TENANT_ID, bill).match_status == "no_counterpart"

    detail = desk.correct(bill, "po_no", ORDER_NO)

    assert (detail.match_status, detail.counterpart_document_id) == ("match", order)
    assert desk.review.detail(DEFAULT_TENANT_ID, order).counterpart_document_id == bill
    assert desk.actions().count("match.created") == 2  # one entry on each document


def test_a_correction_that_names_no_order_links_nothing(desk: Desk) -> None:
    desk.receive_order()
    bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)

    detail = desk.correct(bill, "po_no", "PO-JPH-99999")

    assert detail.match_status == "no_counterpart"


def test_an_order_is_linked_by_hand_when_nothing_on_the_page_links_them(desk: Desk) -> None:
    order = desk.receive_order()
    bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)

    detail = desk.link(bill, order, reason="order placed by phone, number misprinted")

    assert detail.counterpart_document_id == order
    # Linked, and compared as usual: the order number still differs, so it is a mismatch.
    assert detail.match_status == "mismatch"
    assert any(d.invoice_path == "po_no" for d in detail.discrepancies)
    assert desk.actions().count("match.linked") == 2
    with desk.sessions() as session:
        (record,) = session.scalars(select(MatchRecord).where(MatchRecord.manual)).all()
        assert record.data["reason"] == "order placed by phone, number misprinted"
        assert record.data["linked_by"] == "Asha Rao"


def test_linking_by_hand_again_takes_the_newest_link(desk: Desk) -> None:
    first_order = desk.receive_order("first.pdf")
    second_order = desk.receive_order("second.pdf")
    bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)

    desk.link(bill, second_order)
    desk.link(bill, first_order)
    detail = desk.link(bill, second_order)  # back to the one linked before

    assert detail.counterpart_document_id == second_order


def test_order_candidates_put_the_same_supplier_first(desk: Desk) -> None:
    order = desk.receive_order()
    bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)

    (candidate,) = desk.review.order_candidates(DEFAULT_TENANT_ID, bill)

    assert (candidate.document_id, candidate.po_no, candidate.same_supplier) == (
        order,
        ORDER_NO,
        True,
    )


def test_a_link_needs_a_reason_an_order_and_an_unsigned_invoice(desk: Desk) -> None:
    order = desk.receive_order()
    bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)
    other_bill = desk.receive("other.pdf", invoice_no="NVM/26-27/32002")

    with pytest.raises(ValueError, match="reason"):
        desk.link(bill, order, reason="  ")
    with pytest.raises(NotLinkable):
        desk.link(bill, other_bill)  # an invoice is not an order
    with pytest.raises(NotLinkable):
        desk.link(order, bill)  # only an invoice is linked to an order
    with pytest.raises(DocumentNotFound):
        desk.link(bill, uuid.uuid4())
    desk.sign(bill, override_reason="paid by cheque")
    with pytest.raises(AlreadySigned):
        desk.link(bill, order)


def test_another_workspaces_order_cannot_be_linked_or_offered(
    desk: Desk, other_tenant: uuid.UUID
) -> None:
    world = desk.world()
    assert world.service is not None
    theirs = world.service.ingest(
        tenant_id=other_tenant, doc_type="purchase_order", filename="theirs.pdf",
        data=world.order_pdf, actor="api:upload",
    )  # fmt: skip
    assert theirs.version is not None
    assert world.service.process(theirs.version.id) == "succeeded"
    bill = desk.receive("bill.pdf", po_no=MISREAD_ORDER_NO)

    assert desk.review.order_candidates(DEFAULT_TENANT_ID, bill) == []
    with pytest.raises(DocumentNotFound):
        desk.link(bill, theirs.document.id)


# AC-P0-7: what was caught, counted over a range of days, in the caller's workspace only


def test_the_summary_counts_each_flag_in_the_range(desk: Desk) -> None:
    desk.mark_h1()
    desk.receive("INV-2041.pdf", lines__0__expiry=FOUR_MONTHS_LEFT)
    desk.receive("scan.pdf")  # a duplicate of the first, with the same H1 line

    found = desk.review.flag_summary(DEFAULT_TENANT_ID, _today() - timedelta(days=1), _today())

    assert (found.invoices, found.duplicates) == (2, 1)
    assert (found.near_expiry_lines, found.near_expiry_bills) == (1, 1)
    assert (found.schedule_h1_lines, found.schedule_h1_bills) == (2, 2)
    assert found.capped is False


def test_the_summary_follows_corrections(desk: Desk) -> None:
    bill = desk.receive("bill.pdf")
    desk.correct(bill, "lines[2].expiry", "12/26")

    found = desk.review.flag_summary(DEFAULT_TENANT_ID, _today(), _today())

    assert found.near_expiry_lines == 1


def test_the_summary_leaves_out_bills_outside_the_range(desk: Desk, owner_engine: Engine) -> None:
    first = desk.receive("INV-2041.pdf")
    desk.receive("scan.pdf")
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE documents SET created_at = created_at - interval '40 days' WHERE id = :id"
            ),
            {"id": first},
        )

    recent = desk.review.flag_summary(DEFAULT_TENANT_ID, _today() - timedelta(days=30), _today())
    old = desk.review.flag_summary(
        DEFAULT_TENANT_ID, _today() - timedelta(days=45), _today() - timedelta(days=35)
    )

    # The copy is counted in the range it was received in, against an original from before.
    assert (recent.invoices, recent.duplicates) == (1, 1)
    assert (old.invoices, old.duplicates) == (1, 0)


def test_the_summary_never_counts_another_workspaces_bills(
    desk: Desk, other_tenant: uuid.UUID
) -> None:
    for name in ("theirs.pdf", "theirs-again.pdf"):
        world = desk.world(lines__0__expiry=FOUR_MONTHS_LEFT)  # each a file of its own
        assert world.service is not None
        theirs = world.service.ingest(
            tenant_id=other_tenant, doc_type="invoice", filename=name,
            data=world.invoice_pdf, actor="api:upload",
        )  # fmt: skip
        assert theirs.version is not None
        assert world.service.process(theirs.version.id) == "succeeded"
    desk.receive("mine.pdf")

    mine = desk.review.flag_summary(DEFAULT_TENANT_ID, _today(), _today())
    theirs_found = desk.review.flag_summary(other_tenant, _today(), _today())

    assert (mine.invoices, mine.duplicates, mine.near_expiry_lines) == (1, 0, 0)
    assert (theirs_found.invoices, theirs_found.duplicates) == (2, 1)
    assert theirs_found.near_expiry_lines == 2
