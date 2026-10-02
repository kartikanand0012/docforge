"""Documents of more than one page are extracted a page at a time and the pages merged."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.pipeline import ExtractionError, ExtractionPipeline, InvoicePipeline
from docforge.extraction.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTION, build_prompt
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC
from docforge.parsing.base import BBox, Block, Page, ParsedDocument
from fakes import ScriptedProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
BOX = BBox(x0=0, y0=0, x1=10, y1=10)


def document(*pages: tuple[str, ...]) -> ParsedDocument:
    """One text block per string; one tuple of strings per page."""
    blocks, number = [], 0
    for page, texts in enumerate(pages, start=1):
        for text in texts:
            number += 1
            blocks.append(Block(id=f"b{number}", kind="text", text=text, page=page, bbox=BOX))
    return ParsedDocument(
        parser="fake",
        parser_version="0",
        pages=tuple(Page(number=n, width=595, height=842) for n in range(1, len(pages) + 1)),
        blocks=tuple(blocks),
    )


def emptied(value: Any) -> Any:
    """The same reply with every field null and every list empty: a page that shows nothing."""
    if isinstance(value, dict):
        if set(value) == {"text", "block_ids"}:
            return {"text": None, "block_ids": []}
        return {key: emptied(item) for key, item in value.items()}
    return []


@pytest.fixture
def pages(raw_invoice_from_label: RawFromLabel) -> tuple[dict[str, Any], dict[str, Any]]:
    """A perfect reply for pair_001 split as if its table broke after the second line."""
    whole = raw_invoice_from_label(LABEL)
    first, second = copy.deepcopy(whole), emptied(whole)
    first["lines"] = whole["lines"][:2]
    first["totals"] = emptied(whole["totals"])
    second["lines"] = whole["lines"][2:]
    second["totals"] = whole["totals"]
    return first, second


TWO_PAGES = document(("TAX INVOICE", "first page text"), ("second page text", "Grand Total"))


def test_each_page_is_sent_on_its_own_and_the_replies_are_merged(
    pages: tuple[dict[str, Any], dict[str, Any]], raw_invoice_from_label: RawFromLabel
) -> None:
    provider = ScriptedProvider([json.dumps(page) for page in pages])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    whole = raw_invoice_from_label(LABEL)
    extraction = result.extraction
    assert len(result.responses) == 2
    assert extraction.invoice_no.raw == whole["invoice_no"]["text"]
    assert extraction.seller.gstin.raw == whole["seller"]["gstin"]["text"]
    assert [line.batch_no.raw for line in extraction.lines] == [
        line["batch_no"]["text"] for line in whole["lines"]
    ]
    assert extraction.totals.grand_total.raw == whole["totals"]["grand_total"]["text"]
    assert len(extraction.seller.drug_licence_nos) == len(whole["seller"]["drug_licence_nos"])


def test_a_page_request_holds_only_that_pages_blocks_and_says_which_page_it_is(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    provider = ScriptedProvider([json.dumps(page) for page in pages])

    InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    first, second = provider.requests
    assert "first page text" in first.prompt and "second page text" not in first.prompt
    assert "second page text" in second.prompt and "first page text" not in second.prompt
    assert "Page 1 of 2" in first.prompt and "Page 2 of 2" in second.prompt
    assert "[b3] second page text" in second.prompt  # block ids are those of the whole document
    assert first.system == second.system
    assert first.system.startswith(SYSTEM_INSTRUCTION)
    assert "one page" in first.system
    assert first.prompt_version == f"{PROMPT_VERSION}+paged-1"


def test_the_result_records_that_the_paged_prompt_was_used(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    provider = ScriptedProvider([json.dumps(page) for page in pages])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert result.prompt_version == f"{PROMPT_VERSION}+paged-1"


def test_a_single_page_document_is_sent_exactly_as_before(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    """Recorded replies are keyed by the request, so the one-page request must not change."""
    one_page = document(("TAX INVOICE", "text"))
    provider = ScriptedProvider([json.dumps(raw_invoice_from_label(LABEL))])

    result = InvoicePipeline(parser=None, provider=provider).extract(one_page)  # type: ignore[arg-type]

    (request,) = provider.requests
    assert request.system == SYSTEM_INSTRUCTION
    assert request.prompt == build_prompt(one_page)
    assert request.prompt_version == result.prompt_version == PROMPT_VERSION


def test_a_field_printed_on_two_pages_is_taken_from_the_first(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    first, second = pages
    second["invoice_no"] = {"text": "REPEATED-IN-FOOTER", "block_ids": ["b3"]}
    provider = ScriptedProvider([json.dumps(first), json.dumps(second)])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert result.extraction.invoice_no.raw == first["invoice_no"]["text"]


def test_a_malformed_reply_is_retried_for_that_page_only(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    first, second = (json.dumps(page) for page in pages)
    provider = ScriptedProvider([first, "not json", second])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert len(result.responses) == 3
    assert "Page 2 of 2" in provider.requests[2].prompt
    assert "not valid" in provider.requests[2].prompt
    assert len(result.extraction.lines) == len(LABEL["invoice"]["lines"])


def test_a_page_that_fails_twice_fails_the_document_with_every_call_accounted_for(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    provider = ScriptedProvider([json.dumps(pages[0]), "not json", "{}"])

    with pytest.raises(ExtractionError) as raised:
        InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert len(raised.value.responses) == 3


def test_a_page_with_no_text_is_not_sent(pages: tuple[dict[str, Any], dict[str, Any]]) -> None:
    with_blank = document(("TAX INVOICE",), (), ("Grand Total",))
    provider = ScriptedProvider([json.dumps(page) for page in pages])

    result = InvoicePipeline(parser=None, provider=provider).extract(with_blank)  # type: ignore[arg-type]

    assert len(result.responses) == 2
    assert "Page 1 of 3" in provider.requests[0].prompt
    assert "Page 3 of 3" in provider.requests[1].prompt


def test_purchase_orders_are_paged_the_same_way(raw_order_from_label: RawFromLabel) -> None:
    whole = raw_order_from_label(LABEL)
    first, second = copy.deepcopy(whole), emptied(whole)
    first["lines"], second["lines"] = whole["lines"][:1], whole["lines"][1:]
    provider = ScriptedProvider([json.dumps(first), json.dumps(second)])

    result = ExtractionPipeline(None, provider, PURCHASE_ORDER_SPEC).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert result.extraction.po_no.raw == whole["po_no"]["text"]
    assert len(result.extraction.lines) == len(whole["lines"])
    assert provider.requests[0].prompt_version == "purchase-order-v1+paged-1"


def test_two_pages_that_give_different_values_for_one_field_send_the_document_to_review(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    first, second = pages
    second["invoice_no"] = {"text": "ANOTHER-NUMBER", "block_ids": ["b3"]}
    provider = ScriptedProvider([json.dumps(first), json.dumps(second)])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert ("invoice_no", "conflicting_pages") in [
        (issue.path, issue.code) for issue in result.extraction.issues
    ]
    assert result.assessment.decision == "review"


def test_the_same_value_repeated_on_a_later_page_is_not_a_conflict(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    first, second = pages
    second["invoice_no"] = dict(first["invoice_no"])
    provider = ScriptedProvider([json.dumps(first), json.dumps(second)])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert "conflicting_pages" not in [issue.code for issue in result.extraction.issues]


def test_retries_are_limited_for_the_document_not_per_page(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """Twenty pages that each need a retry must not double the cost of the document."""
    many = document(*[(f"text of page {n}",) for n in range(1, 7)])
    provider = ScriptedProvider(["not json", json.dumps(pages[0])] * 6)

    with pytest.raises(ExtractionError, match="too many malformed replies") as raised:
        InvoicePipeline(parser=None, provider=provider).extract(many)  # type: ignore[arg-type]

    assert len(raised.value.responses) == 5  # two pages retried, the third refused


def test_a_carried_forward_total_on_an_earlier_page_is_flagged_not_trusted(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    first, second = pages
    first["totals"]["grand_total"] = {"text": "1,000.00", "block_ids": ["b2"]}
    provider = ScriptedProvider([json.dumps(first), json.dumps(second)])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert "totals.grand_total" in [issue.path for issue in result.extraction.issues]
    assert result.assessment.decision == "review"


def test_licence_numbers_printed_on_different_pages_are_all_kept_once(
    pages: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    first, second = pages
    licences = first["seller"]["drug_licence_nos"]
    extra = {"text": "XX-21B-000001", "block_ids": ["b3"]}
    second["seller"]["drug_licence_nos"] = [licences[0], extra]
    provider = ScriptedProvider([json.dumps(first), json.dumps(second)])

    result = InvoicePipeline(parser=None, provider=provider).extract(TWO_PAGES)  # type: ignore[arg-type]

    assert [licence.raw for licence in result.extraction.seller.drug_licence_nos] == [
        *(licence["text"] for licence in licences),
        "XX-21B-000001",
    ]
