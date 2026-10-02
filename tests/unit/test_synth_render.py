"""Rendered PDFs must carry the ground-truth values at the recorded positions."""

import io
import re
from itertools import combinations

import pytest
from pypdf import PdfReader

from docforge.synth import DEFAULT_COUNT, DEFAULT_SEED
from docforge.synth.builder import build_pair
from docforge.synth.models import DocumentPair, FieldBox, leaf_paths
from docforge.synth.render import RenderedDocument, render_invoice, render_purchase_order

PAIRS = [build_pair(index, DEFAULT_SEED) for index in range(1, DEFAULT_COUNT + 1)]
pair_ids = [pair.pair_id for pair in PAIRS]
RENDERED: list[tuple[str, RenderedDocument]] = [
    (f"{pair.pair_id}-{kind}", document)
    for pair in PAIRS
    for kind, document in (
        ("invoice", render_invoice(pair.invoice, pair.layout)),
        ("po", render_purchase_order(pair.purchase_order, pair.layout)),
    )
]
rendered_ids = [name for name, _ in RENDERED]
DOCUMENTS = [document for _, document in RENDERED]

TextRun = tuple[str, float, float]


def text_runs(pdf: bytes) -> list[TextRun]:
    """Every text run on page 1 with the position it was drawn at (PDF points)."""
    runs: list[TextRun] = []

    def visit(text: str, cm: list[float], tm: list[float], font: object, size: float) -> None:
        if text.strip():
            runs.append((text.strip(), float(tm[4]), float(tm[5])))

    PdfReader(io.BytesIO(pdf)).pages[0].extract_text(visitor_text=visit)
    return runs


def overlaps(a: FieldBox, b: FieldBox) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def test_layouts_under_test_cover_a_and_b() -> None:
    assert {pair.layout for pair in PAIRS} == {"A", "B"}


@pytest.mark.parametrize("document", DOCUMENTS, ids=rendered_ids)
class TestEveryRenderedDocument:
    def test_is_a_single_page_pdf(self, document: RenderedDocument) -> None:
        reader = PdfReader(io.BytesIO(document.pdf))

        assert document.pdf.startswith(b"%PDF-")
        assert len(reader.pages) == 1
        assert float(reader.pages[0].mediabox.width) == pytest.approx(document.page_width)
        assert float(reader.pages[0].mediabox.height) == pytest.approx(document.page_height)

    def test_streams_are_uncompressed_so_bytes_do_not_depend_on_zlib(
        self, document: RenderedDocument
    ) -> None:
        assert b"FlateDecode" not in document.pdf

    def test_boxes_lie_inside_the_page(self, document: RenderedDocument) -> None:
        for box in document.boxes:
            assert box.page == 1
            assert 0 <= box.x0 < box.x1 <= document.page_width, box.path
            assert 0 <= box.y0 < box.y1 <= document.page_height, box.path

    def test_boxes_do_not_overlap(self, document: RenderedDocument) -> None:
        clashes = [(a.path, b.path) for a, b in combinations(document.boxes, 2) if overlaps(a, b)]

        assert clashes == []

    def test_box_paths_are_unique(self, document: RenderedDocument) -> None:
        paths = [box.path for box in document.boxes]

        assert len(set(paths)) == len(paths)

    def test_each_box_contains_its_text_in_the_pdf(self, document: RenderedDocument) -> None:
        runs = text_runs(document.pdf)
        for box in document.boxes:
            inside = [
                text
                for text, x, y in runs
                if box.x0 - 0.5 <= x <= box.x1 and box.y0 - 0.5 <= y <= box.y1
            ]
            assert box.text in inside, f"{box.path}: {box.text!r} not drawn inside its box"


@pytest.mark.parametrize("pair", PAIRS, ids=pair_ids)
def test_every_invoice_label_value_is_boxed_or_known_to_be_unprinted(pair: DocumentPair) -> None:
    """Walks the whole label, so a new field cannot silently go without a box."""
    invoice = pair.invoice
    boxed = {box.path for box in render_invoice(invoice, pair.layout).boxes}

    # Implied by other printed values (GSTIN prefix, place of supply, the tax columns).
    not_printed = {
        "supply_type",
        "place_of_supply_code",
        "seller.state",
        "seller.state_code",
        "buyer.state",
        "buyer.state_code",
    }
    for index in range(len(invoice.lines)):
        not_printed |= {f"lines[{index}].{tax}" for tax in ("cgst", "sgst", "igst")}
    # Only the tax components that apply to the supply type appear in the totals block.
    if invoice.supply_type == "intra_state":
        not_printed.add("totals.igst")
    else:
        not_printed |= {"totals.cgst", "totals.sgst"}

    assert boxed == leaf_paths(invoice) - not_printed
    assert not boxed & not_printed


@pytest.mark.parametrize("pair", PAIRS, ids=pair_ids)
def test_every_order_label_value_is_boxed_or_known_to_be_unprinted(pair: DocumentPair) -> None:
    order = pair.purchase_order
    boxed = {box.path for box in render_purchase_order(order, pair.layout).boxes}

    not_printed = {"buyer.state", "buyer.state_code"}
    not_printed |= {
        f"lines[{index}].scheme" for index, line in enumerate(order.lines) if line.scheme is None
    }

    assert boxed == leaf_paths(order) - not_printed


def test_rendered_document_reports_its_unprinted_paths() -> None:
    pair = PAIRS[0]
    document = render_invoice(pair.invoice, pair.layout)

    assert set(document.unprinted) == leaf_paths(pair.invoice) - {b.path for b in document.boxes}
    assert list(document.unprinted) == sorted(document.unprinted)
    assert "supply_type" in document.unprinted


def test_rendering_is_byte_for_byte_repeatable() -> None:
    pair = PAIRS[0]
    first = render_invoice(pair.invoice, pair.layout)
    second = render_invoice(pair.invoice, pair.layout)

    assert first.pdf == second.pdf


def test_layouts_place_fields_differently() -> None:
    pair = PAIRS[0]
    a = {box.path: box for box in render_invoice(pair.invoice, "A").boxes}
    b = {box.path: box for box in render_invoice(pair.invoice, "B").boxes}

    assert (a["invoice_no"].x0, a["invoice_no"].y0) != (b["invoice_no"].x0, b["invoice_no"].y0)
    assert a["invoice_date"].text != b["invoice_date"].text


def test_dates_are_printed_in_fixed_formats_whatever_the_locale() -> None:
    pair = PAIRS[0]
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    a = {box.path: box.text for box in render_invoice(pair.invoice, "A").boxes}
    b = {box.path: box.text for box in render_invoice(pair.invoice, "B").boxes}
    invoice_date = pair.invoice.invoice_date

    assert re.fullmatch(rf"\d{{2}}-({months})-\d{{4}}", a["invoice_date"])
    assert a["invoice_date"].startswith(f"{invoice_date.day:02d}-")
    assert a["invoice_date"].endswith(f"-{invoice_date.year}")
    assert b["invoice_date"] == (
        f"{invoice_date.day:02d}/{invoice_date.month:02d}/{invoice_date.year}"
    )


def test_layout_a_prints_september_as_sep() -> None:
    pair = next(pair for pair in PAIRS if pair.invoice.invoice_date.month == 9)
    boxes = {box.path: box.text for box in render_invoice(pair.invoice, "A").boxes}

    assert "-Sep-" in boxes["invoice_date"]
