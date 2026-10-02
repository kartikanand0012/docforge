"""The real Docling parser against the synthetic invoices. Slow: loads layout and table models."""

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from docforge.parsing.base import ParsedDocument, ParseError
from docforge.parsing.docling_parser import DoclingParser

pytestmark = pytest.mark.docling

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PAIR_IDS = [f"pair_{index:03d}" for index in range(1, 21)]


@pytest.fixture(scope="module")
def parser() -> DoclingParser:
    return DoclingParser()


@pytest.fixture(scope="module")
def parsed(parser: DoclingParser) -> dict[str, ParsedDocument]:
    return {
        pair_id: parser.parse((FIXTURES / pair_id / "invoice.pdf").read_bytes())
        for pair_id in PAIR_IDS
    }


def test_reports_parser_name_and_version(parsed: dict[str, ParsedDocument]) -> None:
    document = parsed["pair_001"]

    assert document.parser == "docling"
    assert document.parser_version == version("docling")


def test_page_size_matches_the_pdf(parsed: dict[str, ParsedDocument]) -> None:
    label = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
    (page,) = parsed["pair_001"].pages

    assert page.number == 1
    assert page.width == pytest.approx(label["documents"]["invoice"]["page_width"], abs=0.01)
    assert page.height == pytest.approx(label["documents"]["invoice"]["page_height"], abs=0.01)


def test_block_ids_are_sequential_in_reading_order(parsed: dict[str, ParsedDocument]) -> None:
    blocks = parsed["pair_001"].blocks

    assert [block.id for block in blocks] == [f"b{n}" for n in range(1, len(blocks) + 1)]
    assert all(block.text.strip() for block in blocks)


def test_table_cells_carry_row_and_column(parsed: dict[str, ParsedDocument]) -> None:
    cells = [block for block in parsed["pair_001"].blocks if block.kind == "table_cell"]
    texts = [block for block in parsed["pair_001"].blocks if block.kind == "text"]

    assert cells
    assert texts
    assert all(c.table is not None and c.row is not None and c.col is not None for c in cells)
    assert all(t.table is None for t in texts)
    assert {c.text for c in cells if c.header} >= {"Batch", "HSN", "Amount"}
    assert not any(c.header for c in cells if c.row and c.row > 0)


@pytest.mark.parametrize("pair_id", PAIR_IDS)
def test_every_labelled_value_is_inside_a_block_at_its_position(
    parsed: dict[str, ParsedDocument], pair_id: str
) -> None:
    """The ground-truth boxes come from the generator, so this checks text and coordinates."""
    label = json.loads((FIXTURES / pair_id / "label.json").read_text(encoding="utf-8"))
    blocks = parsed[pair_id].blocks

    missing = []
    for box in label["documents"]["invoice"]["boxes"]:
        centre_x = (box["x0"] + box["x1"]) / 2
        centre_y = (box["y0"] + box["y1"]) / 2
        if not any(
            box["text"] in block.text and block.bbox.contains(centre_x, centre_y, tolerance=1)
            for block in blocks
        ):
            missing.append((box["path"], box["text"]))

    assert missing == []


def test_parsing_the_same_file_twice_gives_the_same_blocks(
    parser: DoclingParser, parsed: dict[str, ParsedDocument]
) -> None:
    again = parser.parse((FIXTURES / "pair_002" / "invoice.pdf").read_bytes())

    assert again == parsed["pair_002"]


def test_unreadable_pdf_raises_parse_error(parser: DoclingParser) -> None:
    with pytest.raises(ParseError):
        parser.parse(b"%PDF-1.4\nnot really a pdf")
