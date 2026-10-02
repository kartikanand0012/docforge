"""Rendered PDFs must carry the ground-truth values at the recorded positions."""

import io
from itertools import combinations

import pytest
from pypdf import PdfReader

from docforge.synth import DEFAULT_SEED
from docforge.synth.builder import build_pair
from docforge.synth.models import DocumentPair, FieldBox
from docforge.synth.render import RenderedDocument, render_invoice, render_purchase_order

# The first six pairs include both layouts and both supply types.
PAIRS = [build_pair(index, DEFAULT_SEED) for index in range(1, 7)]
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

INVOICE_LINE_FIELDS = (
    "product_name",
    "pack",
    "hsn",
    "batch_no",
    "mfg",
    "expiry",
    "qty",
    "free_qty",
    "mrp",
    "ptr",
    "discount_pct",
    "taxable_value",
    "gst_rate",
    "amount",
)
INVOICE_HEADER_FIELDS = {
    "invoice_no",
    "invoice_date",
    "po_no",
    "seller.name",
    "seller.gstin",
    "seller.drug_licence_nos[0]",
    "seller.drug_licence_nos[1]",
    "buyer.name",
    "buyer.gstin",
    "buyer.drug_licence_nos[0]",
    "buyer.drug_licence_nos[1]",
    "totals.taxable_value",
    "totals.round_off",
    "totals.grand_total",
}

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


@pytest.mark.parametrize("pair", PAIRS, ids=[pair.pair_id for pair in PAIRS])
def test_invoice_boxes_cover_every_ground_truth_field(pair: DocumentPair) -> None:
    paths = {box.path for box in render_invoice(pair.invoice, pair.layout).boxes}

    expected = set(INVOICE_HEADER_FIELDS)
    expected |= {
        f"lines[{index}].{field}"
        for index in range(len(pair.invoice.lines))
        for field in INVOICE_LINE_FIELDS
    }
    if pair.invoice.supply_type == "intra_state":
        expected |= {"totals.cgst", "totals.sgst"}
    else:
        expected |= {"totals.igst"}

    assert expected <= paths


def test_purchase_order_boxes_cover_header_and_lines() -> None:
    pair = PAIRS[0]
    paths = {box.path for box in render_purchase_order(pair.purchase_order, pair.layout).boxes}

    header = {"po_no", "po_date", "buyer.name", "buyer.gstin", "supplier_name", "supplier_gstin"}
    assert header <= paths
    for index in range(len(pair.purchase_order.lines)):
        line = {f"lines[{index}].{field}" for field in ("product_name", "pack", "qty", "rate")}
        assert line <= paths


def test_scheme_is_printed_only_where_ordered() -> None:
    for pair in PAIRS:
        paths = {box.path for box in render_purchase_order(pair.purchase_order, pair.layout).boxes}
        for index, line in enumerate(pair.purchase_order.lines):
            assert (f"lines[{index}].scheme" in paths) == (line.scheme is not None)


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
