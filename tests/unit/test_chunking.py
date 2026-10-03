"""Documents cut into chunks for search: each one cites the blocks it came from."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from docforge.search.chunking import chunk_document

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.schema import RawInvoice
from fakes import cited

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def chunks(build: RawFromLabel) -> list[Any]:
    raw = build(LABEL)
    parsed = cited(LABEL, "invoice", raw)
    extraction = normalize_invoice(RawInvoice.model_validate(raw), parsed)
    return chunk_document("invoice", "invoice.pdf", parsed, extraction)


def test_the_first_chunk_summarises_the_document_in_words(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    first = chunks(raw_invoice_from_label)[0]
    invoice = LABEL["invoice"]

    assert first.kind == "summary"
    for value in (
        invoice["invoice_no"],
        invoice["seller"]["name"],
        invoice["buyer"]["name"],
        invoice["po_no"],
    ):
        assert value in first.text
    assert "September 2026" in first.text  # the date in words, not only as printed
    assert first.block_ids  # it cites where those values are


def test_every_chunk_cites_blocks_that_exist_and_holds_their_text(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = raw_invoice_from_label(LABEL)
    parsed = cited(LABEL, "invoice", raw)
    blocks = {b.id: b for b in parsed.blocks}

    for chunk in chunks(raw_invoice_from_label)[1:]:
        assert chunk.block_ids and set(chunk.block_ids) <= set(blocks)
        assert chunk.page == blocks[chunk.block_ids[0]].page
        for block_id in chunk.block_ids:
            assert blocks[block_id].text in chunk.text


def test_every_block_is_in_some_chunk(raw_invoice_from_label: RawFromLabel) -> None:
    raw = raw_invoice_from_label(LABEL)
    parsed = cited(LABEL, "invoice", raw)

    covered = {block for chunk in chunks(raw_invoice_from_label)[1:] for block in chunk.block_ids}

    assert covered == {b.id for b in parsed.blocks}


def test_chunks_stay_small_enough_to_cite_precisely(raw_invoice_from_label: RawFromLabel) -> None:
    assert all(len(chunk.text) <= 1200 for chunk in chunks(raw_invoice_from_label))


def test_chunking_is_repeatable(raw_invoice_from_label: RawFromLabel) -> None:
    assert chunks(raw_invoice_from_label) == chunks(raw_invoice_from_label)
