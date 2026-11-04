"""Finding the invoices in one PDF from what its pages print (V2 P6, AC-P6-3/4/6/7).

The PDFs are the committed synthetic invoices, read for their real text and positions; the
ground truth is each fixture's label.
"""

from collections.abc import Callable
from typing import Any

import pytest

from docforge.evals.long_document import long_invoice
from docforge.parsing.base import BBox, Block, Page, ParsedDocument
from docforge.parsing.pdf import pdf_page_count
from docforge.split.detect import (
    COULD_NOT_DETECT,
    PagesDoNotReconcile,
    Proposal,
    evidence,
    propose,
    reconcile,
    undetected,
)
from docforge.split.pages import cut
from split_fixtures import Fixture, bundle, fixture, letter, text_parse

Raw = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture(scope="module")
def three(raw_invoice_from_label: Raw) -> list[Fixture]:
    return [fixture(f"pair_00{n}", raw_invoice_from_label) for n in (1, 2, 3)]


@pytest.fixture(scope="module")
def long_101(raw_invoice_from_label: Raw) -> Fixture:
    return fixture("pair_101", raw_invoice_from_label)


def reconciles(proposal: Proposal) -> None:
    """AC-P6-6: parent pages = invoice pages + non-invoice pages, every page once."""
    placed = [p for g in proposal.groups for p in g.pages] + list(proposal.non_invoice)
    assert sorted(placed) == list(range(1, proposal.page_count + 1))
    invoice_pages = sum(len(g.pages) for g in proposal.groups)
    assert proposal.page_count == invoice_pages + len(proposal.non_invoice)


def test_three_invoices_are_found_certainly_with_their_numbers(three: list[Fixture]) -> None:
    pdf = bundle(*(f.pdf for f in three))

    proposal = propose(text_parse(pdf))

    assert proposal is not None and proposal.certain
    assert [g.pages for g in proposal.groups] == [(1,), (2,), (3,)]
    assert [g.invoice_no for g in proposal.groups] == [f.invoice_no for f in three]
    assert [g.seller_gstin for g in proposal.groups] == [
        f.label["invoice"]["seller"]["gstin"] for f in three
    ]
    reconciles(proposal)


def test_a_continuation_page_without_a_header_is_not_guessed(
    three: list[Fixture], long_101: Fixture
) -> None:
    pdf = bundle(long_101.pdf, three[0].pdf)  # 101 runs over two pages, its second bare

    proposal = propose(text_parse(pdf))

    assert proposal is not None and not proposal.certain
    assert [g.pages for g in proposal.groups] == [(1, 2), (3,)]  # where it most likely goes
    assert proposal.pages_in_doubt == (2,)
    assert "page 2 has no invoice number" in (proposal.reason or "")
    reconciles(proposal)


def test_a_covering_letter_is_proposed_as_not_an_invoice_and_asked_about(
    three: list[Fixture],
) -> None:
    pdf = bundle(letter(), three[0].pdf, three[1].pdf)

    proposal = propose(text_parse(pdf))

    assert proposal is not None and not proposal.certain
    assert proposal.non_invoice == (1,)
    assert [g.pages for g in proposal.groups] == [(2,), (3,)]
    reconciles(proposal)


def test_a_header_printed_on_every_page_keeps_the_pages_together(three: list[Fixture]) -> None:
    pdf = bundle(three[0].pdf, three[1].pdf, pages=[(0, 1), (0, 1), (1, 1)])

    proposal = propose(text_parse(pdf))

    assert proposal is not None and proposal.certain
    assert [g.pages for g in proposal.groups] == [(1, 2), (3,)]
    reconciles(proposal)


@pytest.mark.parametrize("name", ["pair_101", "pair_102", "pair_103", "pair_001"])
def test_one_invoice_is_not_split(name: str, raw_invoice_from_label: Raw) -> None:
    """AC-P6-7: the committed multipage invoices are one invoice each."""
    assert propose(text_parse(fixture(name, raw_invoice_from_label).pdf)) is None


def test_an_eight_page_invoice_is_not_split() -> None:
    pdf = cut(long_invoice(8), list(range(1, 9)))
    assert pdf_page_count(pdf) == 8

    assert propose(text_parse(pdf)) is None


def _page(number: int, *texts: tuple[str, float]) -> list[Block]:
    return [
        Block(id=f"p{number}b{i}", kind="text", text=text, page=number,
              bbox=BBox(x0=40, y0=y, x1=300, y1=y + 9))
        for i, (text, y) in enumerate(texts)
    ]  # fmt: skip


def _parsed(*pages: list[Block]) -> ParsedDocument:
    return ParsedDocument(
        parser="t", parser_version="0",
        pages=tuple(Page(number=n, width=595, height=842) for n in range(1, len(pages) + 1)),
        blocks=tuple(b for page in pages for b in page),
    )  # fmt: skip


GSTIN_A, GSTIN_B = "24AABFN7754K1Z0", "27AAACS1234F1ZS"


def test_page_n_of_m_continues_an_invoice_certainly() -> None:
    parsed = _parsed(
        _page(1, (f"GSTIN: {GSTIN_A}", 800), ("Invoice No.: A-101", 780), ("Page 1 of 2", 30)),
        _page(2, ("Sl. Product Qty", 800), ("continued", 400), ("Page 2 of 2", 30)),
        _page(3, (f"GSTIN: {GSTIN_B}", 800), ("Bill No: B/77", 780)),
    )

    proposal = propose(parsed)

    assert proposal is not None and proposal.certain
    assert [g.pages for g in proposal.groups] == [(1, 2), (3,)]
    reconciles(proposal)


def test_an_invoice_number_low_on_a_page_never_starts_an_invoice() -> None:
    parsed = _parsed(
        _page(1, (f"GSTIN: {GSTIN_A}", 800), ("Invoice No.: A-101", 780)),
        _page(2, (f"GSTIN: {GSTIN_A}", 800), ("Against Invoice No. A-100", 100)),
        _page(3, (f"GSTIN: {GSTIN_B}", 800), ("Inv. Number INV/2026/9", 760)),
    )

    proposal = propose(parsed)

    assert proposal is not None and not proposal.certain
    assert [g.pages for g in proposal.groups] == [(1, 2), (3,)]
    assert "mentions invoice A-100" in (proposal.reason or "")
    reconciles(proposal)


def test_the_same_number_from_another_seller_is_another_invoice() -> None:
    parsed = _parsed(
        _page(1, (f"GSTIN: {GSTIN_A}", 800), ("Invoice No.: 17", 780)),
        _page(2, (f"GSTIN: {GSTIN_B}", 800), ("Invoice No.: 17", 780)),
    )

    proposal = propose(parsed)

    assert proposal is not None and proposal.certain
    assert [g.pages for g in proposal.groups] == [(1,), (2,)]


def test_labels_and_values_are_read_whether_joined_or_apart() -> None:
    pages = evidence(
        _parsed(
            _page(1, (GSTIN_A, 800), ("Invoice No.:", 780), ("NVM/26-27/32001", 770)),
            _page(2, (GSTIN_A, 800), ("Tax Invoice No: 2026-0012", 780)),
            _page(3, (GSTIN_A, 800), ("Invoice Date: 02/09/2026", 780)),
            _page(4, ("Invoice No.: X-1", 780)),  # no GSTIN on the page: no header
        )
    )

    assert [p.invoice_no for p in pages] == ["NVM/26-27/32001", "2026-0012", None, None]
    assert pages[3].stray_no == "X-1"


def test_reconcile_names_what_is_wrong_in_words() -> None:
    reconcile(4, [[1, 2], [3]], [4])
    with pytest.raises(PagesDoNotReconcile, match="Page 4 is in no invoice"):
        reconcile(4, [[1, 2], [3]], [])
    with pytest.raises(PagesDoNotReconcile, match="Page 2 is placed twice"):
        reconcile(4, [[1, 2], [2, 3]], [4])
    with pytest.raises(PagesDoNotReconcile, match="there is no page 5"):
        reconcile(4, [[1, 2], [3, 5]], [4])
    with pytest.raises(PagesDoNotReconcile, match="An invoice has no pages"):
        reconcile(4, [[1, 2, 3, 4], []], [])


def test_undetected_offers_every_page_as_one_invoice_with_the_reason() -> None:
    proposal = undetected(60)

    assert proposal.reason == COULD_NOT_DETECT and not proposal.certain
    assert proposal.groups[0].pages == tuple(range(1, 61))
    reconciles(proposal)


def test_cut_keeps_the_pages_asked_for_in_order(three: list[Fixture]) -> None:
    pdf = bundle(*(f.pdf for f in three))

    child = cut(pdf, [3, 1])

    texts = [" ".join(b.text for b in text_parse(child).blocks if b.page == n) for n in (1, 2)]
    assert three[2].invoice_no in texts[0] and three[0].invoice_no in texts[1]
