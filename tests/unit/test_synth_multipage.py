"""Long invoices: the line table runs over several pages and the label says which page."""

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from docforge.parsing.pdf import pdf_page_count
from docforge.synth import DEFAULT_SEED
from docforge.synth.builder import build_pair, compute_totals
from docforge.synth.dataset import MULTIPAGE_LINE_COUNTS, generate_multipage
from docforge.synth.render import render_invoice, render_purchase_order

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "multipage"
LONG = build_pair(101, DEFAULT_SEED, line_count=45)
INVOICE = render_invoice(LONG.invoice, LONG.layout)


def runs_on_page(pdf: bytes, page: int) -> list[tuple[str, float, float]]:
    found: list[tuple[str, float, float]] = []

    def visit(text: str, cm: list[float], tm: list[float], font: object, size: float) -> None:
        if text.strip():
            found.append((text.strip(), float(tm[4]), float(tm[5])))

    PdfReader(io.BytesIO(pdf)).pages[page - 1].extract_text(visitor_text=visit)
    return found


def test_a_long_pair_has_the_requested_number_of_lines_on_both_documents() -> None:
    assert len(LONG.invoice.lines) == 45
    assert len(LONG.purchase_order.lines) == 45
    assert [line.sl_no for line in LONG.invoice.lines] == list(range(1, 46))
    assert len({line.batch_no for line in LONG.invoice.lines}) == 45
    assert LONG.invoice.totals == compute_totals(LONG.invoice.lines)


def test_asking_for_no_line_count_gives_the_same_pair_as_before() -> None:
    assert build_pair(1, DEFAULT_SEED) == build_pair(1, DEFAULT_SEED, line_count=None)
    assert 3 <= len(build_pair(1, DEFAULT_SEED).invoice.lines) <= 10


def test_a_long_invoice_runs_over_several_pages() -> None:
    pages = pdf_page_count(INVOICE.pdf)

    assert pages >= 2
    assert {box.page for box in INVOICE.boxes} == set(range(1, pages + 1))


def test_lines_fill_the_pages_in_order_and_stay_inside_the_margins() -> None:
    line_pages = [
        next(box.page for box in INVOICE.boxes if box.path == f"lines[{index}].batch_no")
        for index in range(45)
    ]

    assert line_pages == sorted(line_pages)
    assert all(
        box.y0 >= 20 and box.y1 <= INVOICE.page_height - 20 and 0 <= box.x0 <= INVOICE.page_width
        for box in INVOICE.boxes
    )


def test_every_page_repeats_the_table_header() -> None:
    for page in range(1, pdf_page_count(INVOICE.pdf) + 1):
        texts = {text for text, _, _ in runs_on_page(INVOICE.pdf, page)}
        assert {"Batch", "HSN", "Amount"} <= texts


def test_the_header_fields_are_on_the_first_page_and_the_totals_on_the_last() -> None:
    pages = {box.path: box.page for box in INVOICE.boxes}

    assert pages["invoice_no"] == 1
    assert pages["seller.gstin"] == 1
    assert pages["totals.grand_total"] == pdf_page_count(INVOICE.pdf)


def test_values_on_later_pages_are_printed_where_their_boxes_say() -> None:
    last_page = pdf_page_count(INVOICE.pdf)
    runs = runs_on_page(INVOICE.pdf, last_page)
    boxes = [box for box in INVOICE.boxes if box.page == last_page]

    assert boxes
    for box in boxes:
        assert any(
            text == box.text and box.x0 - 0.5 <= x <= box.x1 and box.y0 - 0.5 <= y <= box.y1
            for text, x, y in runs
        ), box.path


def test_boxes_on_the_same_page_do_not_overlap() -> None:
    by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for box in INVOICE.boxes:
        by_page.setdefault(box.page, []).append((box.x0, box.y0, box.x1, box.y1))
    for boxes in by_page.values():
        boxes.sort()
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                if b[0] >= a[2]:
                    break
                assert not (a[1] < b[3] and b[1] < a[3])


def test_a_long_purchase_order_also_runs_over_pages() -> None:
    order = render_purchase_order(LONG.purchase_order, LONG.layout)

    assert pdf_page_count(order.pdf) >= 2
    assert len({box.path for box in order.boxes if box.path.endswith(".qty")}) == 45


def test_the_multipage_set_is_written_with_a_manifest(tmp_path: Path) -> None:
    labels = generate_multipage(tmp_path, seed=DEFAULT_SEED)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pairs"] == [label.pair_id for label in labels]
    assert [len(label.invoice.lines) for label in labels] == list(MULTIPAGE_LINE_COUNTS.values())
    assert {label.layout for label in labels} == {"A", "B"}


@pytest.mark.parametrize("name", ["invoice.pdf", "purchase_order.pdf", "label.json"])
def test_the_committed_multipage_set_matches_a_fresh_generation(tmp_path: Path, name: str) -> None:
    generate_multipage(tmp_path, seed=DEFAULT_SEED)

    for index in MULTIPAGE_LINE_COUNTS:
        pair = f"pair_{index:03d}"
        assert (tmp_path / pair / name).read_bytes() == (FIXTURES / pair / name).read_bytes()
