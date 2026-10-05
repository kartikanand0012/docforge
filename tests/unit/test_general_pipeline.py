"""General documents (contracts, SOPs, manuals, slides): read and indexed for search and chat,
with no field extraction, so no model call and nothing for a person to check."""

from pathlib import Path

import pytest

from docforge.extraction.general import GENERAL_SCHEMA_VERSION, GeneralExtraction, GeneralPipeline
from docforge.parsing.base import BBox, Block, DocumentTooLarge, NoTextLayer, ParsedDocument
from docforge.search.chunking import chunk_document
from docforge.stages import stage_listener
from fakes import PARSED, FakeParser

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()


class Parsed:
    name = "fixed"
    version = "0"

    def __init__(self, parsed: ParsedDocument) -> None:
        self.parsed = parsed

    def parse(self, pdf: bytes) -> ParsedDocument:
        return self.parsed


def block(text: str) -> Block:
    return Block(id="b1", kind="text", text=text, page=1, bbox=BBox(x0=0, y0=0, x1=1, y1=1))


def test_a_general_document_is_parsed_with_no_model_call() -> None:
    parser = FakeParser()
    result = GeneralPipeline(parser).run(PDF)

    assert parser.calls == 1
    assert result.responses == ()
    assert result.parsed is PARSED
    assert result.schema_version == GENERAL_SCHEMA_VERSION
    assert result.prompt_version == "none"


def test_it_records_the_first_line_as_its_title_and_counts_pages_and_words() -> None:
    extraction = GeneralPipeline(FakeParser()).run(PDF).extraction

    assert isinstance(extraction, GeneralExtraction)
    assert extraction.title == "text 1"
    assert extraction.pages == 1
    assert extraction.words == 6  # "text 1", "text 2", "text 3"


def test_a_long_first_line_is_cut_for_the_title() -> None:
    parsed = PARSED.model_copy(update={"blocks": (block("Standard operating procedure " * 20),)})
    title = GeneralPipeline(Parsed(parsed)).run(PDF).extraction.title
    assert title is not None and len(title) <= 120 and title.endswith("…")


def test_nothing_needs_a_person() -> None:
    assessment = GeneralPipeline(FakeParser()).run(PDF).assessment
    assert assessment.decision == "accept"
    assert assessment.fields == () and assessment.rules == () and assessment.reasons == ()


def test_it_reports_no_extracting_or_checking_stage() -> None:
    reached: list[str] = []
    with stage_listener(reached.append):
        GeneralPipeline(FakeParser()).run(PDF)
    assert "extracting" not in reached and "checking" not in reached


def test_a_document_with_no_text_fails_clearly() -> None:
    empty = PARSED.model_copy(update={"blocks": ()})
    with pytest.raises(NoTextLayer):
        GeneralPipeline(Parsed(empty)).run(PDF)


def test_too_many_pages_are_refused_before_parsing() -> None:
    parser = FakeParser()
    with pytest.raises(DocumentTooLarge):
        GeneralPipeline(parser, max_pages=0).run(PDF)
    assert parser.calls == 0


def test_a_general_document_is_chunked_by_its_text_with_a_plain_summary() -> None:
    extraction = GeneralPipeline(FakeParser()).run(PDF).extraction
    chunks = chunk_document("general", "sop-goods-receipt.docx", PARSED, extraction)

    summary = chunks[0]
    assert summary.kind == "summary"
    assert summary.text == "Document sop-goods-receipt.docx: text 1."
    assert summary.block_ids == ("b1",)
    assert [c.kind for c in chunks[1:]] == ["text"]
    assert chunks[1].block_ids == ("b1", "b2", "b3")
