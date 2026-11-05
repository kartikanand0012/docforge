# ruff: noqa: RUF001 - the arithmetic is printed with real minus and multiplication signs
"""The PO variance of one invoice: per line, what the order agreed, what the bill charged, and
the money difference, with the arithmetic shown (V2 P7)."""

import copy
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.purchase_order import RawPurchaseOrder, normalize_purchase_order
from docforge.extraction.schema import RawInvoice
from docforge.trust.variance import Tolerances, compare, inr
from variance_fixtures import NO_BLOCKS, bill, billed, order, ordered

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PAIR_IDS = [f"pair_{n:03d}" for n in range(1, 21)]
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def test_ac_p7_1_a_rate_above_the_order_is_priced_on_the_billed_quantity() -> None:
    found = compare(bill(billed(ptr="105.00")), order(ordered(rate="100.00")))

    (line,) = found.differing
    (rate,) = line.differences
    assert (rate.field, rate.label, rate.status) == ("rate", "Rate", "variance")
    assert (rate.po_value, rate.billed_value, rate.difference) == (
        Decimal("100.00"), Decimal("105.00"), Decimal("5.00"),
    )  # fmt: skip
    assert rate.money_impact == Decimal("1000.00")
    assert isinstance(rate.money_impact, Decimal)
    assert rate.working == "(105.00 − 100.00) × 200 billed = ₹1,000.00"
    assert found.net_impact == Decimal("1000.00")
    assert found.po_total == Decimal("20000.00")
    assert found.net_percent == Decimal("5.00")
    assert found.matched_lines == 0


def test_ac_p7_2_missing_scheme_goods_are_free_qty_valued_at_ptr_never_qty() -> None:
    found = compare(bill(billed(qty=100, free_qty=0)), order(ordered(qty=100, scheme="10+1")))

    (line,) = found.differing
    (free,) = line.differences
    assert (free.field, free.label) == ("free_qty", "Free Qty")
    assert (free.po_value, free.billed_value, free.difference) == (10, 0, -10)
    assert free.money_impact == Decimal("1000.00")
    assert free.working == "(10 due − 0 billed) × PTR 100.00 = ₹1,000.00"
    assert "10+1" in (free.note or "")
    assert [d.field for d in line.differences] == ["free_qty"]  # not merged into Qty


@pytest.mark.parametrize("pair_id", PAIR_IDS)
def test_ac_p7_3_a_matching_pair_has_no_differing_line(
    pair_id: str, raw_invoice_from_label: RawFromLabel, raw_order_from_label: RawFromLabel
) -> None:
    label = json.loads((FIXTURES / pair_id / "label.json").read_text(encoding="utf-8"))
    invoice = normalize_invoice(
        RawInvoice.model_validate(copy.deepcopy(raw_invoice_from_label(label))), NO_BLOCKS
    )
    purchase = normalize_purchase_order(
        RawPurchaseOrder.model_validate(copy.deepcopy(raw_order_from_label(label))), NO_BLOCKS
    )

    found = compare(invoice, purchase)

    assert found.differing == ()
    assert found.matched_lines == len(invoice.lines) == len(found.lines)
    assert (found.not_in_po, found.not_billed) == ((), ())
    assert found.net_impact == Decimal("0.00")


def test_ac_p7_3_a_rounding_difference_inside_the_tolerance_is_matched_with_a_note() -> None:
    invoice = bill(billed(ptr="100.40"))
    purchase = order(ordered(rate="100.00"))

    exact = compare(invoice, purchase)
    tolerant = compare(invoice, purchase, Tolerances(rate=Decimal("1.00")))

    assert exact.differing[0].differences[0].money_impact == Decimal("80.00")
    assert tolerant.differing == ()
    assert tolerant.matched_lines == 1
    (rate,) = tolerant.lines[0].differences
    assert rate.status == "within_tolerance"
    assert rate.money_impact is None
    assert rate.note == "Rate differs by 0.40, inside the workspace's tolerance of 1.00."
    assert tolerant.net_impact == Decimal("0.00")


def test_a_taxable_amount_within_one_rupee_is_matched_by_default() -> None:
    found = compare(bill(billed(taxable_value="20000.40")), order(ordered()))

    assert found.differing == ()
    (taxable,) = found.lines[0].differences
    assert (taxable.field, taxable.status) == ("taxable", "within_tolerance")


def test_a_taxable_amount_beyond_qty_times_rate_is_its_own_variance() -> None:
    found = compare(bill(billed(taxable_value="20100.00")), order(ordered()))

    (taxable,) = found.differing[0].differences
    assert (taxable.field, taxable.label) == ("taxable", "Taxable Amount")
    assert (taxable.po_value, taxable.billed_value) == (Decimal("20000.00"), Decimal("20100.00"))
    assert taxable.money_impact == Decimal("100.00")
    assert taxable.working == "20,100.00 billed − PO terms 200 × 100.00 = 20,000.00 → ₹100.00"


def test_a_taxable_amount_never_counts_a_rate_difference_twice() -> None:
    found = compare(bill(billed(ptr="105.00", taxable_value="21050.00")), order(ordered()))

    rate, taxable = found.differing[0].differences
    assert rate.money_impact == Decimal("1000.00")
    assert taxable.money_impact == Decimal("50.00")
    assert taxable.working == (
        "21,050.00 billed − PO terms 200 × 100.00 = 20,000.00 → 1,050.00, "
        "less the rate difference 1,000.00 = ₹50.00"
    )
    assert found.net_impact == Decimal("1050.00")


def test_the_bills_own_discount_reduces_the_rate_impact_and_is_shown() -> None:
    found = compare(bill(billed(ptr="105.00", discount_pct="2")), order(ordered()))

    (rate,) = found.differing[0].differences
    assert rate.money_impact == Decimal("980.00")
    assert rate.working == "(105.00 − 100.00) × 200 billed, less 2% discount = ₹980.00"


def test_a_quantity_shortfall_is_priced_at_the_po_rate() -> None:
    found = compare(bill(billed(qty=190)), order(ordered(qty=200)))

    (qty,) = found.differing[0].differences
    assert (qty.field, qty.label, qty.difference) == ("qty", "Qty", -10)
    assert qty.money_impact == Decimal("-1000.00")
    assert qty.working == "(190 billed − 200 ordered) × PO rate 100.00 = −₹1,000.00"
    assert found.above_po == Decimal("0.00")
    assert found.net_impact == Decimal("-1000.00")


def test_a_quantity_inside_its_tolerance_is_matched() -> None:
    found = compare(bill(billed(qty=199)), order(ordered(qty=200)), Tolerances(qty=1))

    assert found.differing == ()
    assert found.lines[0].differences[0].status == "within_tolerance"


def test_ac_p7_4_lines_on_one_side_only_are_listed_apart_never_merged() -> None:
    found = compare(
        bill(billed(), billed("Paracetamol Tablets IP 500mg", qty=50, ptr="12.00")),
        order(ordered(), ordered("Cetirizine Tablets IP 10mg", qty=30, rate="8.50")),
    )

    assert found.differing == ()
    assert found.matched_lines == 1
    (extra,) = found.not_in_po
    assert (extra.product, extra.qty, extra.rate, extra.value) == (
        "Paracetamol Tablets IP 500mg", 50, Decimal("12.00"), Decimal("600.00"),
    )  # fmt: skip
    (missing,) = found.not_billed
    assert (missing.product, missing.qty, missing.rate, missing.value) == (
        "Cetirizine Tablets IP 10mg", 30, Decimal("8.50"), Decimal("255.00"),
    )  # fmt: skip
    assert missing.working == "30 × 8.50 = ₹255.00"
    assert found.net_impact == Decimal("0.00")  # set apart, never in the net
    assert (found.not_in_po_value, found.not_billed_value) == (Decimal("600.00"), Decimal("255.00"))


def test_a_second_batch_of_an_ordered_product_says_the_product_is_on_the_order() -> None:
    found = compare(bill(billed(qty=120), billed(qty=80)), order(ordered(qty=200)))

    (extra,) = found.not_in_po
    assert extra.note == "This product is on the PO, paired with bill line 1 (another batch?)."


def test_a_value_not_read_is_not_compared_and_carries_no_money() -> None:
    found = compare(bill(billed(ptr=None, taxable_value="20000.00")), order(ordered()))

    (line,) = found.differing
    rate = next(d for d in line.differences if d.field == "rate")
    assert rate.status == "not_compared"
    assert rate.money_impact is None
    assert rate.note == "The bill's rate was not read, so it could not be compared."
    assert found.net_impact == Decimal("0.00")
    assert found.not_compared == 1


def test_a_different_pack_is_noted_on_the_line() -> None:
    found = compare(bill(billed(pack="10x15")), order(ordered(pack="10x10")))

    assert found.lines[0].note == "Pack differs: bill 10x15, PO 10x10. Check before acting on it."


def test_the_header_names_the_bill_and_its_order() -> None:
    found = compare(
        bill(billed(ptr="105.00"), invoice_no="NVM/9", grand_total="23520.00"),
        order(ordered(), po_no="PO-1"),
    )

    assert (found.invoice_no, found.po_no) == ("NVM/9", "PO-1")
    assert found.supplier_name == "Navjivan Medical Agencies"
    assert found.supplier_gstin == "24AABFN7754K1Z0"
    assert found.invoice_total == Decimal("23520.00")
    assert found.variances == 1


def test_the_percentage_is_left_out_when_the_po_value_is_unknown() -> None:
    found = compare(bill(billed()), order(ordered(rate=None)))

    assert found.po_total == Decimal("0.00")
    assert found.net_percent is None


@pytest.mark.parametrize(
    ("value", "words"),
    [
        (Decimal("1000"), "₹1,000.00"),
        (Decimal("123456789.5"), "₹12,34,56,789.50"),
        (Decimal("-1000"), "−₹1,000.00"),
        (Decimal("0.004"), "₹0.00"),
    ],
)
def test_rupees_are_written_as_india_reads_them(value: Decimal, words: str) -> None:
    assert inr(value) == words
