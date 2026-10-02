"""The extraction pipeline with a stand-in parser and model."""

import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.pipeline import ExtractionError, InvoicePipeline
from docforge.extraction.prompt import PROMPT_VERSION
from docforge.extraction.schema import InvoiceExtraction
from docforge.llm.base import LLMError
from docforge.parsing.base import ParseError
from fakes import PARSED, FakeParser, ScriptedProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def extract(raw: dict[str, Any]) -> InvoiceExtraction:
    return InvoicePipeline(FakeParser(), ScriptedProvider([json.dumps(raw)])).run(PDF).extraction


def test_returns_a_typed_extraction_with_run_details(raw_invoice_from_label: RawFromLabel) -> None:
    provider = ScriptedProvider([json.dumps(raw_invoice_from_label(LABEL))])

    result = InvoicePipeline(FakeParser(), provider).run(PDF)

    extraction = result.extraction
    assert extraction.invoice_no.value == LABEL["invoice"]["invoice_no"]
    assert extraction.invoice_date.value == date.fromisoformat(LABEL["invoice"]["invoice_date"])
    assert extraction.place_of_supply.value == "Gujarat"
    assert extraction.place_of_supply_code.value == "24"
    assert extraction.seller.gstin.value == LABEL["invoice"]["seller"]["gstin"]
    assert len(extraction.lines) == len(LABEL["invoice"]["lines"])
    assert extraction.lines[0].expiry.value == LABEL["invoice"]["lines"][0]["expiry"]
    assert extraction.lines[0].qty.value == LABEL["invoice"]["lines"][0]["qty"]
    assert extraction.totals.grand_total.value == Decimal(LABEL["invoice"]["totals"]["grand_total"])
    assert extraction.issues == ()
    assert result.parsed == PARSED
    assert [response.model for response in result.responses] == ["fake-1"]


def test_sends_the_versioned_prompt_and_raw_schema(raw_invoice_from_label: RawFromLabel) -> None:
    provider = ScriptedProvider([json.dumps(raw_invoice_from_label(LABEL))])

    InvoicePipeline(FakeParser(), provider).run(PDF)

    (request,) = provider.requests
    assert request.prompt_version == PROMPT_VERSION
    assert "[b1] text 1" in request.prompt
    assert request.schema.__name__ == "RawInvoice"


def test_keeps_the_printed_string_beside_the_typed_value(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    extraction = extract(raw_invoice_from_label(LABEL))

    assert extraction.invoice_date.raw == "02-Sep-2026"
    assert extraction.lines[0].expiry.raw == "06/28"


def test_a_value_that_cannot_be_read_becomes_null_with_an_issue(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = raw_invoice_from_label(LABEL)
    raw["invoice_date"]["text"] = "sometime in September"
    raw["lines"][1]["qty"]["text"] = "two hundred"

    extraction = extract(raw)

    assert extraction.invoice_date.value is None
    assert extraction.invoice_date.raw == "sometime in September"
    assert extraction.lines[1].qty.value is None
    assert {(issue.path, issue.code) for issue in extraction.issues} == {
        ("invoice_date", "unparseable"),
        ("lines[1].qty", "unparseable"),
    }


def test_a_missing_value_is_null_without_an_issue(raw_invoice_from_label: RawFromLabel) -> None:
    raw = raw_invoice_from_label(LABEL)
    raw["po_no"] = {"text": None, "block_ids": []}

    extraction = extract(raw)

    assert extraction.po_no.value is None
    assert extraction.issues == ()


def test_citations_of_blocks_that_do_not_exist_are_dropped_and_reported(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = raw_invoice_from_label(LABEL)
    raw["invoice_no"]["block_ids"] = ["b2", "b99"]

    extraction = extract(raw)

    assert extraction.invoice_no.block_ids == ("b2",)
    assert [(issue.path, issue.code) for issue in extraction.issues] == [
        ("invoice_no", "unknown_block")
    ]


def test_an_invalid_reply_is_retried_once_with_the_errors(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    provider = ScriptedProvider(['{"invoice_no": 5}', json.dumps(raw_invoice_from_label(LABEL))])

    result = InvoicePipeline(FakeParser(), provider).run(PDF)

    assert result.extraction.invoice_no.value == LABEL["invoice"]["invoice_no"]
    assert len(result.responses) == 2
    first, second = provider.requests
    assert second.prompt.startswith(first.prompt)
    assert "not valid" in second.prompt


def test_two_invalid_replies_raise_an_extraction_error() -> None:
    provider = ScriptedProvider(["not json", "{}"])

    with pytest.raises(ExtractionError, match="schema"):
        InvoicePipeline(FakeParser(), provider).run(PDF)

    assert len(provider.requests) == 2


def test_model_errors_propagate() -> None:
    provider = ScriptedProvider([LLMError("quota exhausted")])

    with pytest.raises(LLMError, match="quota"):
        InvoicePipeline(FakeParser(), provider).run(PDF)


def test_too_many_pages_is_rejected_before_parsing() -> None:
    parser = FakeParser()

    with pytest.raises(ParseError, match="pages"):
        InvoicePipeline(parser, ScriptedProvider([]), max_pages=0).run(PDF)

    assert parser.calls == 0


def test_unreadable_pdf_is_rejected_before_parsing() -> None:
    parser = FakeParser()

    with pytest.raises(ParseError):
        InvoicePipeline(parser, ScriptedProvider([])).run(b"not a pdf")

    assert parser.calls == 0
