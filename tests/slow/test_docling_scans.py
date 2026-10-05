"""The real parser on scans (OCR path) and on documents longer than one batch of pages."""

import io
import json
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from docforge.parsing.base import Block, NoTextLayer, ParsedDocument
from docforge.parsing.docling_parser import DoclingParser
from docforge.synth.scans import PROFILES

pytestmark = pytest.mark.docling

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE_NAMES = sorted(PROFILES)


@pytest.fixture(scope="module")
def parser() -> DoclingParser:
    return DoclingParser(batch_pages=2)


@pytest.fixture(scope="module")
def scans(parser: DoclingParser) -> dict[str, ParsedDocument]:
    return {
        name: parser.parse((FIXTURES / "scanned" / name / "pair_001" / "invoice.pdf").read_bytes())
        for name in PROFILE_NAMES
    }


def label_box(profile: str, path: str) -> dict[str, float]:
    label = json.loads(
        (FIXTURES / "scanned" / profile / "pair_001" / "label.json").read_text(encoding="utf-8")
    )
    return next(box for box in label["documents"]["invoice"]["boxes"] if box["path"] == path)


def covering(document: ParsedDocument, box: dict[str, float]) -> list[Block]:
    x, y = (box["x0"] + box["x1"]) / 2, (box["y0"] + box["y1"]) / 2
    return [block for block in document.blocks if block.bbox.contains(x, y, 2.0)]


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_a_scan_is_read_by_ocr_into_blocks(scans: dict[str, ParsedDocument], profile: str) -> None:
    document = scans[profile]

    assert document.source == "ocr"
    assert document.parser == "docling"
    assert [block.id for block in document.blocks] == [
        f"b{n}" for n in range(1, len(document.blocks) + 1)
    ]
    assert "XGX944068" in {block.text for block in document.blocks}


@pytest.mark.parametrize("profile", PROFILE_NAMES)
@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("lines[0].batch_no", "XGX944068"),
        ("totals.grand_total", "98697.00"),
        ("lines[9].amount", "161.75"),
    ],
)
def test_blocks_sit_where_the_ink_is_on_the_page_as_scanned(
    scans: dict[str, ParsedDocument], profile: str, path: str, text: str
) -> None:
    """The poor scan is crooked: it is straightened to be read, and boxes are mapped back."""
    found = covering(scans[profile], label_box(profile, path))

    assert text in {block.text for block in found}


def test_the_table_of_a_crooked_scan_keeps_its_rows(scans: dict[str, ParsedDocument]) -> None:
    cells = [block for block in scans["scan_poor"].blocks if block.kind == "table_cell"]
    batch = next(cell for cell in cells if cell.text == "XGX944068")
    row = {cell.text for cell in cells if (cell.table, cell.row) == (batch.table, batch.row)}

    assert {"30041030", "06/26", "06/28", "1306.36"} <= row


def test_a_born_digital_file_is_read_from_its_text_layer(parser: DoclingParser) -> None:
    document = parser.parse((FIXTURES / "synthetic" / "pair_001" / "invoice.pdf").read_bytes())

    assert document.source == "text_layer"


def test_without_ocr_a_scan_is_refused() -> None:
    scan = (FIXTURES / "scanned" / "scan_good" / "pair_001" / "invoice.pdf").read_bytes()

    with pytest.raises(NoTextLayer):
        DoclingParser(ocr=False).parse(scan)


def numbered_pdf(pages: int) -> bytes:
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=A4, invariant=1)
    for number in range(1, pages + 1):
        page.setFont("Helvetica", 14)
        page.drawString(72, 700, f"This is the text of page number {number} of the document.")
        page.showPage()
    page.save()
    return out.getvalue()


def test_a_document_longer_than_one_batch_keeps_every_page_in_order(
    parser: DoclingParser,
) -> None:
    document = parser.parse(numbered_pdf(5))  # batches of 2: three conversions

    assert [page.number for page in document.pages] == [1, 2, 3, 4, 5]
    assert [block.id for block in document.blocks] == [
        f"b{n}" for n in range(1, len(document.blocks) + 1)
    ]
    for number in range(1, 6):
        texts = [block.text for block in document.blocks if block.page == number]
        assert texts == [f"This is the text of page number {number} of the document."]


def test_batching_does_not_change_the_result(parser: DoclingParser) -> None:
    pdf = numbered_pdf(3)

    assert parser.parse(pdf) == DoclingParser(batch_pages=10).parse(pdf)
