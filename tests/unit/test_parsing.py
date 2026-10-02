from pathlib import Path

import pytest

from docforge.parsing.base import BBox, Block, Page, ParsedDocument, ParseError
from docforge.parsing.pdf import pdf_page_count

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


def block(block_id: str, text: str = "x") -> Block:
    return Block(id=block_id, kind="text", text=text, page=1, bbox=BBox(x0=0, y0=0, x1=10, y1=10))


def document(*blocks: Block) -> ParsedDocument:
    return ParsedDocument(
        parser="fake",
        parser_version="0",
        pages=(Page(number=1, width=595, height=842),),
        blocks=blocks,
    )


def test_block_lookup_by_id() -> None:
    parsed = document(block("b1", "first"), block("b2", "second"))

    found = parsed.block("b2")

    assert found is not None
    assert found.text == "second"
    assert parsed.block("b9") is None


def test_duplicate_block_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate block id"):
        document(block("b1"), block("b1"))


def test_bbox_must_have_positive_area() -> None:
    with pytest.raises(ValueError, match="bbox"):
        BBox(x0=10, y0=0, x1=10, y1=5)


def test_bbox_contains_point() -> None:
    box = BBox(x0=10, y0=20, x1=30, y1=40)

    assert box.contains(20, 30)
    assert not box.contains(31, 30)
    assert box.contains(31, 30, tolerance=1)


def test_table_cell_needs_its_table_coordinates() -> None:
    with pytest.raises(ValueError, match="table, row and col"):
        Block(id="b1", kind="table_cell", text="x", page=1, bbox=BBox(x0=0, y0=0, x1=1, y1=1))


def test_page_count_of_a_fixture_invoice() -> None:
    pdf = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()

    assert pdf_page_count(pdf) == 1


@pytest.mark.parametrize("data", [b"", b"not a pdf at all", b"%PDF-1.4\ntruncated"])
def test_page_count_rejects_unreadable_pdfs(data: bytes) -> None:
    with pytest.raises(ParseError):
        pdf_page_count(data)
