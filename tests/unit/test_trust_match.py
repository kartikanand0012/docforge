"""Matching an invoice against its purchase order."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.purchase_order import RawPurchaseOrder, normalize_purchase_order
from docforge.extraction.schema import RawInvoice
from docforge.parsing.base import Page, ParsedDocument
from docforge.trust.match import Discrepancy, match_invoice_to_order

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PAIR_IDS = [f"pair_{n:03d}" for n in range(1, 21)]
NO_BLOCKS = ParsedDocument(
    parser="fake", parser_version="0", pages=(Page(number=1, width=1, height=1),), blocks=()
)
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def label(pair_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / pair_id / "label.json").read_text(encoding="utf-8")
    )
    return loaded


class Pair:
    def __init__(self, pair_id: str, invoice: RawFromLabel, order: RawFromLabel) -> None:
        self.invoice = copy.deepcopy(invoice(label(pair_id)))
        self.order = copy.deepcopy(order(label(pair_id)))

    def match(self) -> tuple[Discrepancy, ...]:
        return match_invoice_to_order(
            normalize_invoice(RawInvoice.model_validate(self.invoice), NO_BLOCKS),
            normalize_purchase_order(RawPurchaseOrder.model_validate(self.order), NO_BLOCKS),
        )

    def codes(self) -> list[str]:
        return [discrepancy.code for discrepancy in self.match()]


@pytest.fixture
def pair(raw_invoice_from_label: RawFromLabel, raw_order_from_label: RawFromLabel) -> Pair:
    return Pair("pair_001", raw_invoice_from_label, raw_order_from_label)


@pytest.mark.parametrize("pair_id", PAIR_IDS)
def test_a_correct_pair_has_no_discrepancies(
    pair_id: str, raw_invoice_from_label: RawFromLabel, raw_order_from_label: RawFromLabel
) -> None:
    assert Pair(pair_id, raw_invoice_from_label, raw_order_from_label).match() == ()


def test_a_different_quantity_is_reported_with_both_values(pair: Pair) -> None:
    pair.invoice["lines"][0]["qty"]["text"] = "25"

    (found,) = [d for d in pair.match() if d.code == "line.qty"]

    assert found.severity == "error"
    assert (found.invoice_path, found.order_path) == ("lines[0].qty", "lines[0].qty")
    assert (found.invoice_value, found.order_value) == ("25", "20")


def test_a_different_rate_is_reported(pair: Pair) -> None:
    pair.invoice["lines"][1]["ptr"]["text"] = "99.99"

    assert "line.rate" in pair.codes()


def test_free_quantity_that_does_not_follow_the_ordered_scheme_is_reported(pair: Pair) -> None:
    schemed = next(i for i, line in enumerate(pair.order["lines"]) if line["scheme"]["text"])
    pair.invoice["lines"][schemed]["free_qty"]["text"] = "0"

    (found,) = [d for d in pair.match() if d.code == "line.free_qty"]

    assert found.invoice_path == f"lines[{schemed}].free_qty"


def test_free_goods_without_an_ordered_scheme_are_reported(pair: Pair) -> None:
    plain = next(i for i, line in enumerate(pair.order["lines"]) if not line["scheme"]["text"])
    pair.invoice["lines"][plain]["free_qty"]["text"] = "3"

    assert "line.free_qty" in pair.codes()


def test_a_product_that_was_not_ordered_is_reported(pair: Pair) -> None:
    pair.invoice["lines"][0]["product_name"]["text"] = "Something Else Tablets 10mg"

    codes = pair.codes()

    assert "line.not_ordered" in codes
    assert "line.not_supplied" in codes


def test_lines_are_matched_by_product_not_by_position(pair: Pair) -> None:
    pair.invoice["lines"].reverse()

    assert pair.match() == ()


def test_a_different_order_number_is_reported(pair: Pair) -> None:
    pair.invoice["po_no"]["text"] = "PO-OTHER-1"

    assert pair.codes() == ["po_no"]


def test_a_different_supplier_or_buyer_is_reported(pair: Pair) -> None:
    pair.order["supplier_gstin"]["text"] = "27AAPFU0939F1ZV"
    pair.order["buyer"]["gstin"]["text"] = "29AAGCB7383J1Z4"

    assert set(pair.codes()) == {"supplier.gstin", "buyer.gstin"}


def test_an_order_dated_after_its_invoice_is_reported(pair: Pair) -> None:
    pair.order["po_date"]["text"] = "03-Sep-2026"  # the invoice is dated 02-Sep-2026

    assert pair.codes() == ["po_date"]


def test_a_missing_value_is_reported_as_unchecked_not_as_a_mismatch(pair: Pair) -> None:
    pair.invoice["lines"][0]["qty"] = {"text": None, "block_ids": []}

    (found,) = pair.match()

    assert found.code == "line.unchecked"
    assert found.invoice_path == "lines[0].qty"


def test_two_lines_of_the_same_product_are_each_paired(pair: Pair) -> None:
    """The same product in two batches is two lines on both documents."""
    for document in (pair.invoice, pair.order):
        document["lines"].append(copy.deepcopy(document["lines"][0]))
    pair.invoice["lines"][-1]["qty"]["text"] = "40"
    pair.order["lines"][-1]["qty"]["text"] = "40"

    assert pair.match() == ()


def test_product_names_are_paired_whatever_their_case_or_spacing(pair: Pair) -> None:
    name = pair.invoice["lines"][0]["product_name"]["text"]
    pair.invoice["lines"][0]["product_name"]["text"] = name.upper().replace(" ", "  ")

    assert pair.match() == ()


def test_an_ordered_line_missing_from_the_invoice_needs_review(pair: Pair) -> None:
    pair.invoice["lines"].pop()

    (found,) = pair.match()

    assert (found.code, found.severity) == ("line.not_supplied", "error")


def test_a_value_that_cannot_be_compared_needs_review(pair: Pair) -> None:
    pair.invoice["lines"][0]["qty"] = {"text": None, "block_ids": []}

    assert {d.severity for d in pair.match()} == {"error"}


def test_a_blank_free_quantity_against_an_ordered_scheme_is_reported(pair: Pair) -> None:
    schemed = next(i for i, line in enumerate(pair.order["lines"]) if line["scheme"]["text"])
    pair.invoice["lines"][schemed]["free_qty"] = {"text": None, "block_ids": []}

    assert "line.free_qty" in pair.codes()


def test_two_lines_of_one_product_pair_with_the_order_line_they_agree_with(pair: Pair) -> None:
    """Same product, same quantity, different rates, listed in the opposite order."""
    for document in (pair.invoice, pair.order):
        document["lines"].append(copy.deepcopy(document["lines"][0]))
    pair.invoice["lines"][-1]["ptr"]["text"] = "1.00"
    pair.order["lines"][0]["rate"]["text"] = "1.00"

    assert pair.match() == ()


def test_an_order_line_with_no_readable_product_is_reported(pair: Pair) -> None:
    pair.order["lines"][0]["product_name"] = {"text": None, "block_ids": []}

    found = pair.match()

    assert ("line.unchecked", "error", "lines[0].product_name") in [
        (d.code, d.severity, d.order_path) for d in found
    ]


def printed(text: str | None) -> dict[str, Any]:
    return {"text": text, "block_ids": []}


def test_an_order_total_equal_to_the_invoice_total_with_or_without_tax_agrees(
    pair: Pair,
) -> None:
    for total in ("grand_total", "taxable_value"):
        pair.order["total"] = printed(pair.invoice["totals"][total]["text"])

        assert pair.match() == (), total


def test_an_order_total_unlike_both_invoice_totals_is_a_warning_with_both_values(
    pair: Pair,
) -> None:
    pair.order["total"] = printed("1.00")

    (found,) = pair.match()

    assert (found.code, found.severity) == ("order.total", "warning")
    assert (found.invoice_path, found.order_path) == ("totals.grand_total", "total")
    assert found.order_value == "1.00"


def test_an_order_without_a_total_is_not_compared(pair: Pair) -> None:
    pair.order["total"] = printed(None)

    assert pair.match() == ()


def test_a_different_hsn_on_a_matched_line_is_a_warning(pair: Pair) -> None:
    billed = pair.invoice["lines"][0]["hsn"]["text"]
    pair.order["lines"][0]["hsn"] = printed(billed)
    pair.order["lines"][1]["hsn"] = printed("99999999")

    found = pair.match()

    assert [(d.code, d.severity) for d in found] == [("line.hsn", "warning")]
    assert (found[0].invoice_path, found[0].order_path) == ("lines[1].hsn", "lines[1].hsn")
    assert found[0].order_value == "99999999"
