"""Corrections, confirmations, signatures and the approval draft, without a database."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.pipeline import INVOICE_SPEC
from docforge.extraction.schema import InvoiceExtraction, RawInvoice
from docforge.review.revise import Correction, apply_corrections, reassess
from docforge.review.signing import approval_draft, hash_pin, record_hash, verify_pin
from fakes import cited

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def raw(raw_invoice_from_label: RawFromLabel) -> RawInvoice:
    return RawInvoice.model_validate(raw_invoice_from_label(LABEL))


def test_a_correction_replaces_the_printed_text_and_keeps_the_citation(raw: RawInvoice) -> None:
    before = raw.lines[1].batch_no

    corrected = apply_corrections(raw, [Correction(path="lines[1].batch_no", text="WE8365")])

    assert isinstance(corrected, RawInvoice)
    assert corrected.lines[1].batch_no.text == "WE8365"
    assert corrected.lines[1].batch_no.block_ids == before.block_ids
    assert raw.lines[1].batch_no.text == before.text  # the original is untouched


def test_the_latest_correction_of_a_path_wins(raw: RawInvoice) -> None:
    corrected = apply_corrections(
        raw,
        [Correction(path="invoice_no", text="A-1"), Correction(path="invoice_no", text="A-2")],
    )

    assert corrected.invoice_no.text == "A-2"


@pytest.mark.parametrize("path", ["lines[99].qty", "lines[0].colour", "lines", "seller", "x.y"])
def test_a_path_that_is_not_a_printed_field_is_refused(raw: RawInvoice, path: str) -> None:
    with pytest.raises(ValueError, match="not a field"):
        apply_corrections(raw, [Correction(path=path, text="1")])


def test_a_field_can_be_corrected_to_not_printed(raw: RawInvoice) -> None:
    corrected = apply_corrections(raw, [Correction(path="po_date", text=None)])

    assert corrected.po_date.text is None


def test_the_corrected_record_is_read_again_and_its_checks_run_again(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    printed = raw_invoice_from_label(LABEL)
    printed["lines"][0]["qty"]["text"] = "25"  # misread; the arithmetic no longer holds
    parsed = cited(LABEL, "invoice", printed)
    raw = RawInvoice.model_validate(printed)

    before = reassess(INVOICE_SPEC, raw, parsed, [])
    after = reassess(INVOICE_SPEC, raw, parsed, [Correction(path="lines[0].qty", text="20")])

    assert before.assessment.decision == "review"
    assert isinstance(after.extraction, InvoiceExtraction)
    assert after.extraction.lines[0].qty.value == 20
    assert after.assessment.decision == "accept"
    (field,) = [f for f in after.assessment.fields if f.path == "lines[0].qty"]
    assert (field.status, field.needs_review) == ("confirmed", False)


def test_confirming_a_value_without_changing_it_settles_a_citation_doubt(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    printed = raw_invoice_from_label(LABEL)
    parsed = cited(LABEL, "invoice", printed)
    printed["invoice_no"]["block_ids"] = ["b1"]  # cites the wrong block
    raw = RawInvoice.model_validate(printed)
    text = printed["invoice_no"]["text"]

    before = reassess(INVOICE_SPEC, raw, parsed, [])
    after = reassess(INVOICE_SPEC, raw, parsed, [Correction(path="invoice_no", text=text)])

    assert before.assessment.decision == "review"
    assert after.assessment.decision == "accept"


def test_a_correction_that_cannot_be_read_is_reported(raw_invoice_from_label: RawFromLabel) -> None:
    printed = raw_invoice_from_label(LABEL)
    parsed = cited(LABEL, "invoice", printed)

    result = reassess(
        INVOICE_SPEC,
        RawInvoice.model_validate(printed),
        parsed,
        [Correction(path="lines[0].qty", text="twenty")],
    )

    assert isinstance(result.extraction, InvoiceExtraction)
    assert ("lines[0].qty", "unparseable") in [(i.path, i.code) for i in result.extraction.issues]
    assert result.assessment.decision == "review"


def test_a_pin_is_stored_as_a_salted_hash_and_checked() -> None:
    stored = hash_pin("482913")

    assert "482913" not in stored
    assert stored != hash_pin("482913")  # salted
    assert verify_pin("482913", stored)
    assert not verify_pin("482914", stored)
    assert not verify_pin("", stored)


def test_the_record_hash_covers_the_content_not_its_key_order() -> None:
    a = record_hash({"b": 1, "a": [1, 2]}, outcome="approved", meaning="approved for payment")
    b = record_hash({"a": [1, 2], "b": 1}, outcome="approved", meaning="approved for payment")

    assert a == b and len(a) == 64
    assert a != record_hash(
        {"a": [1, 3], "b": 1}, outcome="approved", meaning="approved for payment"
    )
    assert a != record_hash(
        {"b": 1, "a": [1, 2]}, outcome="rejected", meaning="approved for payment"
    )


def test_an_approved_invoice_becomes_a_payment_approval_draft(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    printed = raw_invoice_from_label(LABEL)
    parsed = cited(LABEL, "invoice", printed)
    extraction = reassess(INVOICE_SPEC, RawInvoice.model_validate(printed), parsed, []).extraction
    assert isinstance(extraction, InvoiceExtraction)
    signed_at = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)

    draft = approval_draft(
        extraction, reviewer="A. Reviewer", signed_at=signed_at, record_sha256="f" * 64
    )

    invoice = LABEL["invoice"]
    assert draft["type"] == "payment_approval_draft"
    assert draft["status"] == "draft"  # nothing is paid: a person or a system acts on it
    assert draft["payee"] == {
        "name": invoice["seller"]["name"],
        "gstin": invoice["seller"]["gstin"],
    }
    assert draft["invoice_no"] == invoice["invoice_no"]
    assert draft["amount"] == invoice["totals"]["grand_total"]
    assert draft["po_no"] == invoice["po_no"]
    assert draft["approved_by"] == "A. Reviewer"
    assert draft["approved_at"] == "2026-10-03T09:30:00+00:00"
    assert draft["record_sha256"] == "f" * 64
