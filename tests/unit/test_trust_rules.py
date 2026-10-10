"""Deterministic invoice rules: silent on correct documents, specific on defective ones."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.schema import InvoiceExtraction, RawInvoice
from docforge.parsing.base import Page, ParsedDocument
from docforge.trust.invoice_rules import INVOICE_RULES
from docforge.trust.rules import RuleResult, run_rules

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PAIR_IDS = [f"pair_{n:03d}" for n in range(1, 21)]
NO_BLOCKS = ParsedDocument(
    parser="fake", parser_version="0", pages=(Page(number=1, width=1, height=1),), blocks=()
)
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
Change = Callable[[dict[str, Any]], None]


def label(pair_id: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / pair_id / "label.json").read_text(encoding="utf-8")
    )
    return loaded


def extraction(raw: dict[str, Any]) -> InvoiceExtraction:
    return normalize_invoice(RawInvoice.model_validate(raw), NO_BLOCKS)


def failures(results: tuple[RuleResult, ...]) -> list[tuple[str, tuple[str, ...]]]:
    return [(result.rule_id, result.paths) for result in results if result.outcome == "failed"]


def run(raw: dict[str, Any]) -> tuple[RuleResult, ...]:
    return run_rules(INVOICE_RULES, extraction(raw))


@pytest.mark.parametrize("pair_id", PAIR_IDS)
def test_no_rule_fails_on_a_correct_invoice(
    pair_id: str, raw_invoice_from_label: RawFromLabel
) -> None:
    results = run(raw_invoice_from_label(label(pair_id)))

    assert failures(results) == []
    assert {result.outcome for result in results} == {"passed"}


def test_every_rule_has_an_id_a_version_and_a_severity(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    results = run(raw_invoice_from_label(label("pair_001")))

    assert {result.severity for result in results} <= {"error", "warning"}
    assert all(result.version >= 1 and result.rule_id and result.message for result in results)
    assert {result.rule_id for result in results} >= {
        "gstin.checksum",
        "line.not_expired",
        "line.taxable_value",
        "line.amount",
        "line.ptr_not_above_mrp",
        "totals.taxable_value",
        "totals.grand_total",
        "tax.matches_supply_type",
    }


def set_line(index: int, name: str, text: str) -> Change:
    def change(raw: dict[str, Any]) -> None:
        raw["lines"][index][name]["text"] = text

    return change


def set_total(name: str, text: str | None) -> Change:
    def change(raw: dict[str, Any]) -> None:
        raw["totals"][name]["text"] = text

    return change


def set_seller_gstin(raw: dict[str, Any]) -> None:
    raw["seller"]["gstin"]["text"] = raw["seller"]["gstin"]["text"][:-1] + "9"


# pair_001: intra-state (Gujarat), invoice dated 2026-09-02, line 0 = 20 x 59.51 less 2%, 12% GST.
DEFECTS: list[tuple[str, Change, str, tuple[str, ...]]] = [
    ("line amount", set_line(0, "amount", "1316.36"), "line.amount", ("lines[0].amount",)),
    (
        "line taxable value",
        set_line(0, "taxable_value", "1190.20"),
        "line.taxable_value",
        ("lines[0].taxable_value",),
    ),
    ("expired stock", set_line(2, "expiry", "08/26"), "line.not_expired", ("lines[2].expiry",)),
    (
        "expiry before manufacture",
        set_line(1, "mfg", "05/29"),
        "line.dates",
        ("lines[1].mfg", "lines[1].expiry"),
    ),
    ("PTR above MRP", set_line(3, "ptr", "150.00"), "line.ptr_not_above_mrp", ("lines[3].ptr",)),
    ("HSN format", set_line(0, "hsn", "3004-10"), "hsn.format", ("lines[0].hsn",)),
    (
        "grand total",
        set_total("grand_total", "98797.00"),
        "totals.grand_total",
        ("totals.grand_total",),
    ),
    (
        "taxable total",
        set_total("taxable_value", "90777.93"),
        "totals.taxable_value",
        ("totals.taxable_value",),
    ),
    ("GSTIN check character", set_seller_gstin, "gstin.checksum", ("seller.gstin",)),
    (
        "IGST on an intra-state invoice",
        set_total("igst", "8019.30"),
        "tax.matches_supply_type",
        ("totals.igst",),
    ),
]


@pytest.mark.parametrize(
    ("change", "rule_id", "paths"),
    [defect[1:] for defect in DEFECTS],
    ids=[defect[0] for defect in DEFECTS],
)
def test_a_defect_fails_the_rule_that_names_it(
    change: Change, rule_id: str, paths: tuple[str, ...], raw_invoice_from_label: RawFromLabel
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    change(raw)

    failed = failures(run(raw))

    assert (rule_id, paths) in failed


def test_one_wrong_line_amount_does_not_fail_unrelated_lines(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    set_line(0, "amount", "1316.36")(raw)

    failed = failures(run(raw))

    assert [rule for rule, paths in failed if any("lines[" in path for path in paths)] == [
        "line.amount"
    ]


def test_a_rule_with_a_missing_input_is_not_evaluated_rather_than_failed(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    raw["lines"][0]["qty"] = {"text": None, "block_ids": []}
    raw["totals"]["grand_total"] = {"text": None, "block_ids": []}

    results = run(raw)

    outcomes = {(result.rule_id, result.paths[0]): result.outcome for result in results}
    assert outcomes[("line.taxable_value", "lines[0].taxable_value")] == "not_evaluated"
    assert outcomes[("totals.grand_total", "totals.grand_total")] == "not_evaluated"
    assert ("line.taxable_value", ("lines[0].taxable_value",)) not in failures(results)


def test_stock_close_to_expiry_is_a_warning_not_an_error(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    set_line(0, "expiry", "11/26")(raw)  # two months after the invoice date

    results = [result for result in run(raw) if result.outcome == "failed"]

    assert [(result.rule_id, result.severity) for result in results] == [
        ("line.shelf_life", "warning")
    ]


def test_an_inter_state_invoice_must_not_carry_cgst(raw_invoice_from_label: RawFromLabel) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_003")))  # inter-state
    raw["totals"]["cgst"] = {"text": "10.00", "block_ids": []}

    assert ("tax.matches_supply_type", ("totals.cgst",)) in failures(run(raw))


def test_every_line_must_carry_its_batch_number(raw_invoice_from_label: RawFromLabel) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    raw["lines"][1]["batch_no"] = {"text": None, "block_ids": []}

    assert ("required.present", ("lines[1].batch_no",)) in failures(run(raw))


def test_a_quantity_printed_with_a_zero_fraction_is_read_as_the_count(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    count = raw["lines"][0]["qty"]["text"]
    raw["lines"][0]["qty"]["text"] = f"{count}.00"

    invoice = extraction(raw)

    assert invoice.lines[0].qty.value == int(count)
    assert invoice.issues == ()
    assert failures(run_rules(INVOICE_RULES, invoice)) == []


def test_a_fractional_quantity_is_reported_as_unreadable_not_as_missing(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    raw["lines"][0]["qty"]["text"] = "7.5"

    invoice = extraction(raw)
    results = run_rules(INVOICE_RULES, invoice)

    assert invoice.lines[0].qty.value is None
    assert [(issue.path, issue.code) for issue in invoice.issues] == [
        ("lines[0].qty", "unparseable")
    ]
    [required] = [
        r for r in results if r.rule_id == "required.present" and r.paths == ("lines[0].qty",)
    ]
    assert required.outcome == "failed"
    assert required.message == "lines[0].qty is printed as '7.5' but could not be read."


def test_a_blank_discount_is_read_as_no_discount(raw_invoice_from_label: RawFromLabel) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_002")))  # line 0 has no discount
    raw["lines"][0]["discount_pct"] = {"text": None, "block_ids": []}

    assert failures(run(raw)) == []


def test_line_tax_must_be_split_within_a_state_and_whole_between_states(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    # 10 x 10.02 at 5%: split halves round to 2.51 each (105.22); one whole tax is 5.01 (105.21).
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))  # intra-state
    line = raw["lines"][0]
    for name, text in (("qty", "10"), ("ptr", "10.02"), ("discount_pct", "0"), ("gst_rate", "5")):
        line[name]["text"] = text
    line["taxable_value"]["text"] = "100.20"
    line["amount"]["text"] = "105.24"

    assert ("line.amount", ("lines[0].amount",)) in failures(run(raw))
    line["amount"]["text"] = "105.22"
    assert ("line.amount", ("lines[0].amount",)) not in failures(run(raw))


def without_batches_or_expiry(raw: dict[str, Any]) -> None:
    for line in raw["lines"]:
        for name in ("batch_no", "mfg", "expiry"):
            line[name] = {"text": None, "block_ids": []}


def test_an_invoice_that_prints_no_batch_or_expiry_is_not_asked_for_them(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    # A general goods invoice (a camera, not a medicine) has no batch or expiry column.
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    without_batches_or_expiry(raw)

    results = run(raw)

    required = {r.paths[0] for r in results if r.rule_id == "required.present"}
    assert not any(path.endswith((".batch_no", ".expiry")) for path in required)
    assert "lines[0].qty" in required and "lines[0].product_name" in required
    assert [path for rule, (path, *_) in failures(results) if rule == "required.present"] == []


def test_once_one_line_prints_an_expiry_every_line_needs_its_batch_and_expiry(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    without_batches_or_expiry(raw)
    raw["lines"][0]["expiry"] = {"text": "06/28", "block_ids": []}

    failed = failures(run(raw))

    assert ("required.present", ("lines[0].batch_no",)) in failed
    assert ("required.present", ("lines[1].batch_no",)) in failed
    assert ("required.present", ("lines[1].expiry",)) in failed
    assert ("required.present", ("lines[0].expiry",)) not in failed


def test_a_batch_printed_but_unreadable_still_counts_as_printed(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = copy.deepcopy(raw_invoice_from_label(label("pair_001")))
    without_batches_or_expiry(raw)
    raw["lines"][0]["expiry"] = {"text": "13/99", "block_ids": []}  # not a month

    failed = failures(run(raw))

    assert ("required.present", ("lines[0].expiry",)) in failed
    assert ("required.present", ("lines[1].batch_no",)) in failed
