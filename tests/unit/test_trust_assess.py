"""The assessment: what was checked, what failed, and whether a person must look."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.schema import RawInvoice
from docforge.parsing.base import ParsedDocument
from docforge.trust.assess import Assessment, assess
from docforge.trust.invoice_rules import INVOICE_RULES
from docforge.trust.verify import extracted_fields
from fakes import cited, reprint

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PAIR_IDS = [f"pair_{n:03d}" for n in range(1, 21)]
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def label(pair_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / pair_id / "label.json").read_text(encoding="utf-8")
    )
    return loaded


class Case:
    def __init__(self, pair_id: str, build: RawFromLabel) -> None:
        self.raw = copy.deepcopy(build(label(pair_id)))
        self.parsed: ParsedDocument = cited(label(pair_id), "invoice", self.raw)

    def reprint(self, path: str, text: str) -> None:
        self.parsed = reprint(self.parsed, self.raw, path, text)

    def assess(self) -> Assessment:
        extraction = normalize_invoice(RawInvoice.model_validate(self.raw), self.parsed)
        return assess(extraction, self.parsed, INVOICE_RULES)


@pytest.fixture
def case(raw_invoice_from_label: RawFromLabel) -> Case:
    return Case("pair_001", raw_invoice_from_label)


@pytest.mark.parametrize("pair_id", PAIR_IDS)
def test_a_correct_fully_cited_invoice_is_accepted(
    pair_id: str, raw_invoice_from_label: RawFromLabel
) -> None:
    result = Case(pair_id, raw_invoice_from_label).assess()

    assert (result.decision, result.reasons) == ("accept", ())
    assert not any(field.needs_review for field in result.fields)


def test_every_extracted_value_links_to_a_page_box(case: Case) -> None:
    """The first half of the C3 gate."""
    result = case.assess()
    extraction = normalize_invoice(RawInvoice.model_validate(case.raw), case.parsed)
    with_values = {path for path, field in extracted_fields(extraction) if field.raw is not None}

    assert {field.path for field in result.fields} == with_values
    assert all(field.boxes for field in result.fields)
    assert all(box.page == 1 and box.x1 > box.x0 for field in result.fields for box in field.boxes)


def test_a_wrong_batch_number_sends_the_document_to_review(case: Case) -> None:
    """The model returns a batch number that is not what the page says."""
    case.raw["lines"][0]["batch_no"]["text"] = "XGX944O68"

    result = case.assess()

    assert result.decision == "review"
    assert result.reasons == ("1 value was not found in the source text it cites",)
    (flagged,) = [field for field in result.fields if field.needs_review]
    assert (flagged.path, flagged.status) == ("lines[0].batch_no", "not_in_cited_blocks")
    assert flagged.reasons == ("not found in the cited source text",)


def test_bad_arithmetic_printed_on_the_document_sends_it_to_review(case: Case) -> None:
    case.reprint("lines[0].amount", "1316.36")

    result = case.assess()

    assert result.decision == "review"
    assert result.reasons == ("2 checks failed: line.amount, totals.tax",)
    flagged = {field.path: field.reasons for field in result.fields if field.needs_review}
    assert flagged["lines[0].amount"] == ("failed line.amount",)
    assert all(field.status == "verified" for field in result.fields)  # it was read correctly


def test_expired_stock_sends_the_document_to_review(case: Case) -> None:
    case.reprint("lines[2].expiry", "08/26")

    result = case.assess()

    assert result.decision == "review"
    assert result.reasons == ("1 check failed: line.not_expired",)


def test_a_warning_is_recorded_but_does_not_force_review(case: Case) -> None:
    case.reprint("lines[0].expiry", "11/26")

    result = case.assess()

    assert result.decision == "accept"
    warnings = [rule for rule in result.rules if rule.outcome == "failed"]
    assert [(rule.rule_id, rule.severity) for rule in warnings] == [("line.shelf_life", "warning")]


def test_a_value_that_could_not_be_read_sends_the_document_to_review(case: Case) -> None:
    case.reprint("invoice_date", "second of September")

    result = case.assess()

    assert result.decision == "review"
    assert "1 value could not be read" in result.reasons
    assert [issue.path for issue in result.issues] == ["invoice_date"]


def test_a_missing_required_value_sends_the_document_to_review(case: Case) -> None:
    case.raw["invoice_no"] = {"text": None, "block_ids": []}

    result = case.assess()

    assert result.decision == "review"
    assert result.reasons == ("1 check failed: required.present",)


def test_an_uncited_value_sends_the_document_to_review(case: Case) -> None:
    case.raw["po_no"]["block_ids"] = []

    result = case.assess()

    assert result.decision == "review"
    assert result.reasons == ("1 value was not found in the source text it cites",)
    (flagged,) = [field for field in result.fields if field.needs_review]
    assert (flagged.path, flagged.reasons) == ("po_no", ("no source cited",))
    assert flagged.found_in  # the reviewer is told where the value does appear
