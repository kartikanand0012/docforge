"""The synthetic pairs must be internally consistent: they are the eval ground truth."""

import re
from decimal import ROUND_HALF_UP, Decimal

import pytest

from docforge.gstin import is_valid_gstin
from docforge.synth import DEFAULT_COUNT, DEFAULT_SEED
from docforge.synth.builder import build_pair
from docforge.synth.models import DocumentPair, Party

CENT = Decimal("0.01")
PAIRS = [build_pair(index, DEFAULT_SEED) for index in range(1, DEFAULT_COUNT + 1)]
pair_ids = [pair.pair_id for pair in PAIRS]


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def test_same_seed_and_index_give_the_same_pair() -> None:
    assert build_pair(3, DEFAULT_SEED) == build_pair(3, DEFAULT_SEED)


def test_different_seed_gives_a_different_pair() -> None:
    assert build_pair(3, DEFAULT_SEED) != build_pair(3, DEFAULT_SEED + 1)


def test_index_must_be_positive() -> None:
    with pytest.raises(ValueError, match="index"):
        build_pair(0, DEFAULT_SEED)


def test_pair_ids_are_sequential() -> None:
    assert pair_ids[:2] == ["pair_001", "pair_002"]
    assert len(set(pair_ids)) == DEFAULT_COUNT


def test_invoice_numbers_are_unique_across_the_set() -> None:
    assert len({pair.invoice.invoice_no for pair in PAIRS}) == DEFAULT_COUNT


def test_set_covers_both_supply_types_layouts_and_schemes() -> None:
    assert {pair.invoice.supply_type for pair in PAIRS} == {"intra_state", "inter_state"}
    assert {pair.layout for pair in PAIRS} == {"A", "B"}
    assert any(line.free_qty > 0 for pair in PAIRS for line in pair.invoice.lines)
    assert any(line.free_qty == 0 for pair in PAIRS for line in pair.invoice.lines)


@pytest.mark.parametrize("pair", PAIRS, ids=pair_ids)
class TestEveryPair:
    def test_has_between_three_and_ten_lines(self, pair: DocumentPair) -> None:
        assert 3 <= len(pair.invoice.lines) <= 10
        assert [line.sl_no for line in pair.invoice.lines] == list(
            range(1, len(pair.invoice.lines) + 1)
        )

    def test_party_identifiers_are_well_formed(self, pair: DocumentPair) -> None:
        parties: list[Party] = [pair.invoice.seller, pair.invoice.buyer]
        for party in parties:
            assert is_valid_gstin(party.gstin)
            assert party.gstin[:2] == party.state_code
            assert len(party.drug_licence_nos) == 2
        assert pair.invoice.seller.gstin != pair.invoice.buyer.gstin

    def test_supply_type_follows_state_codes(self, pair: DocumentPair) -> None:
        invoice = pair.invoice
        same_state = invoice.seller.state_code == invoice.buyer.state_code

        assert invoice.supply_type == ("intra_state" if same_state else "inter_state")
        assert invoice.place_of_supply_code == invoice.buyer.state_code

    def test_line_arithmetic_is_exact(self, pair: DocumentPair) -> None:
        intra = pair.invoice.supply_type == "intra_state"
        zero = Decimal("0.00")
        for line in pair.invoice.lines:
            gross = line.qty * line.ptr
            taxable = money(gross * (Decimal(100) - line.discount_pct) / Decimal(100))
            assert line.taxable_value == taxable
            if intra:
                half = money(taxable * line.gst_rate / Decimal(200))
                assert (line.cgst, line.sgst, line.igst) == (half, half, zero)
            else:
                full = money(taxable * line.gst_rate / Decimal(100))
                assert (line.cgst, line.sgst, line.igst) == (zero, zero, full)
            assert line.amount == line.taxable_value + line.cgst + line.sgst + line.igst

    def test_totals_are_the_sum_of_the_lines(self, pair: DocumentPair) -> None:
        lines, totals = pair.invoice.lines, pair.invoice.totals

        assert totals.taxable_value == sum(line.taxable_value for line in lines)
        assert totals.cgst == sum(line.cgst for line in lines)
        assert totals.sgst == sum(line.sgst for line in lines)
        assert totals.igst == sum(line.igst for line in lines)

    def test_grand_total_is_rounded_to_the_rupee(self, pair: DocumentPair) -> None:
        totals = pair.invoice.totals
        unrounded = totals.taxable_value + totals.cgst + totals.sgst + totals.igst

        assert totals.grand_total == totals.grand_total.to_integral_value()
        assert totals.grand_total == unrounded + totals.round_off
        assert abs(totals.round_off) <= Decimal("0.50")

    def test_prices_are_plausible(self, pair: DocumentPair) -> None:
        for line in pair.invoice.lines:
            assert Decimal(0) < line.ptr < line.mrp
            assert line.qty > 0
            assert Decimal(0) <= line.discount_pct < Decimal(100)
            assert line.gst_rate in {Decimal(5), Decimal(12), Decimal(18)}

    def test_dates_are_ordered(self, pair: DocumentPair) -> None:
        invoice = pair.invoice
        invoice_month = invoice.invoice_date.strftime("%Y-%m")

        assert pair.purchase_order.po_date <= invoice.invoice_date
        for line in invoice.lines:
            assert line.mfg < invoice_month < line.expiry

    def test_batch_and_hsn_formats(self, pair: DocumentPair) -> None:
        batches = [line.batch_no for line in pair.invoice.lines]

        assert len(set(batches)) == len(batches)
        for line in pair.invoice.lines:
            assert re.fullmatch(r"[A-Z]{2,3}\d{4,6}", line.batch_no)
            assert re.fullmatch(r"30\d{6}", line.hsn)
            assert re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", line.expiry)
            assert re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", line.mfg)

    def test_invoice_agrees_with_its_purchase_order(self, pair: DocumentPair) -> None:
        invoice, order = pair.invoice, pair.purchase_order

        assert invoice.po_no == order.po_no
        assert invoice.po_date == order.po_date
        assert order.buyer == invoice.buyer
        assert order.supplier_name == invoice.seller.name
        assert order.supplier_gstin == invoice.seller.gstin
        assert len(order.lines) == len(invoice.lines)
        for ordered, billed in zip(order.lines, invoice.lines, strict=True):
            assert ordered.product_name == billed.product_name
            assert ordered.pack == billed.pack
            assert ordered.qty == billed.qty
            assert ordered.rate == billed.ptr

    def test_free_quantity_follows_the_ordered_scheme(self, pair: DocumentPair) -> None:
        for ordered, billed in zip(pair.purchase_order.lines, pair.invoice.lines, strict=True):
            if ordered.scheme is None:
                assert billed.free_qty == 0
            else:
                buy, free = (int(part) for part in ordered.scheme.split("+"))
                assert billed.free_qty == (billed.qty // buy) * free
