"""The certificate-of-analysis document type: extraction and its own checks."""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.coa import COA_SPEC, CoaExtraction, RawCoa
from docforge.parsing.base import Page, ParsedDocument
from docforge.trust.rules import run_rules

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coa"
CASES = sorted(p.name for p in FIXTURES.glob("coa_*"))
NO_BLOCKS = ParsedDocument(
    parser="fake", parser_version="0", pages=(Page(number=1, width=1, height=1),), blocks=()
)
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def label(case: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / case / "label.json").read_text(encoding="utf-8")
    )
    return loaded


def extract(raw: dict[str, Any]) -> CoaExtraction:
    extraction = COA_SPEC.normalize(RawCoa.model_validate(raw), NO_BLOCKS)
    assert isinstance(extraction, CoaExtraction)
    return extraction


def failed(extraction: CoaExtraction) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (r.rule_id, r.paths) for r in run_rules(COA_SPEC.rules, extraction) if r.outcome == "failed"
    ]


def test_a_certificate_becomes_a_typed_record(raw_coa_from_label: RawFromLabel) -> None:
    truth = label("coa_001")["coa"]

    coa = extract(raw_coa_from_label(label("coa_001")))

    assert coa.schema_version == "coa-1"
    assert coa.batch_no.value == truth["batch_no"]
    assert coa.product_name.value == truth["product_name"]
    assert coa.mfg.value == truth["mfg"] and coa.expiry.value == truth["expiry"]
    assert coa.analysis_date.value == date.fromisoformat(truth["analysis_date"])
    assert [t.result.value for t in coa.tests] == [t["result"] for t in truth["tests"]]
    assert coa.issues == ()
    assert COA_SPEC.doc_type == "coa" and COA_SPEC.prompt_version == "coa-v1"


@pytest.mark.parametrize("case", CASES)
def test_each_certificate_is_flagged_exactly_where_it_was_seeded(
    case: str, raw_coa_from_label: RawFromLabel
) -> None:
    truth = label(case)
    coa = extract(raw_coa_from_label(truth))
    names = [t["name"] for t in truth["coa"]["tests"]]

    found = failed(coa)

    expected = [
        ("coa.result_within_limit", (f"tests[{names.index(n)}].result",))
        for n in truth["out_of_limit"]
    ]
    if truth["out_of_limit"] and "complies with" in truth["coa"]["conclusion"]:
        expected.append(("coa.conclusion_consistent", ("conclusion",)))
    assert sorted(found) == sorted(expected)


def test_a_result_that_cannot_be_read_needs_a_person(raw_coa_from_label: RawFromLabel) -> None:
    raw = raw_coa_from_label(label("coa_001"))
    raw["tests"][3]["result"] = {"text": "see attached", "block_ids": []}

    results = run_rules(COA_SPEC.rules, extract(raw))

    (unread,) = [r for r in results if r.paths == ("tests[3].result",)]
    assert (unread.outcome, unread.severity) == ("not_evaluated", "error")


def test_expiry_before_manufacture_is_flagged(raw_coa_from_label: RawFromLabel) -> None:
    raw = raw_coa_from_label(label("coa_001"))
    raw["expiry"] = {"text": "01/20", "block_ids": []}

    assert ("coa.dates", ("mfg", "expiry")) in failed(extract(raw))
