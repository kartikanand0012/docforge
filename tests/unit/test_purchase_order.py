"""The purchase-order document type, through the same pipeline as invoices."""

import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline
from docforge.extraction.purchase_order import (
    PURCHASE_ORDER_SPEC,
    PurchaseOrderExtraction,
    RawPurchaseOrder,
    normalize_purchase_order,
    order_rules,
)
from docforge.parsing.base import Page, ParsedDocument
from fakes import FakeParser, ScriptedProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "purchase_order.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]
NO_BLOCKS = ParsedDocument(
    parser="fake", parser_version="0", pages=(Page(number=1, width=1, height=1),), blocks=()
)


def extract(raw: dict[str, Any]) -> PurchaseOrderExtraction:
    provider = ScriptedProvider([json.dumps(raw)])
    return ExtractionPipeline(FakeParser(), provider, PURCHASE_ORDER_SPEC).run(PDF).extraction


def test_a_purchase_order_becomes_a_typed_record(raw_order_from_label: RawFromLabel) -> None:
    order = extract(raw_order_from_label(LABEL))
    truth = LABEL["purchase_order"]

    assert order.schema_version == "purchase-order-2"
    assert order.po_no.value == truth["po_no"]
    assert order.po_date.value == date.fromisoformat(truth["po_date"])
    assert order.buyer.gstin.value == truth["buyer"]["gstin"]
    assert order.supplier_name.value == truth["supplier_name"]
    assert order.supplier_gstin.value == truth["supplier_gstin"]
    assert len(order.lines) == len(truth["lines"])
    for extracted, expected in zip(order.lines, truth["lines"], strict=True):
        assert extracted.product_name.value == expected["product_name"]
        assert extracted.qty.value == expected["qty"]
        assert extracted.rate.value == Decimal(expected["rate"])
        assert extracted.scheme.value == expected["scheme"]
    assert order.issues == ()


def test_an_order_quantity_printed_with_a_zero_fraction_is_read_as_the_count(
    raw_order_from_label: RawFromLabel,
) -> None:
    raw = raw_order_from_label(LABEL)
    for line in raw["lines"]:
        line["qty"]["text"] = f"{line['qty']['text']}.00"

    order = extract(raw)

    assert [line.qty.value for line in order.lines] == [
        line["qty"] for line in LABEL["purchase_order"]["lines"]
    ]
    assert order.issues == ()


def test_the_run_records_which_prompt_and_schema_were_used(
    raw_order_from_label: RawFromLabel,
) -> None:
    provider = ScriptedProvider([json.dumps(raw_order_from_label(LABEL))])

    result = ExtractionPipeline(FakeParser(), provider, PURCHASE_ORDER_SPEC).run(PDF)

    assert result.prompt_version == "purchase-order-v2"
    assert result.schema_version == "purchase-order-2"
    assert provider.requests[0].schema.__name__ == "RawPurchaseOrder"
    assert "purchase order" in provider.requests[0].system.lower()


def test_a_scheme_that_is_not_buy_plus_free_is_reported(raw_order_from_label: RawFromLabel) -> None:
    raw = raw_order_from_label(LABEL)
    raw["lines"][0]["scheme"] = {"text": "ten percent extra", "block_ids": []}

    order = extract(raw)

    assert order.lines[0].scheme.value is None
    assert [(issue.path, issue.code) for issue in order.issues] == [
        ("lines[0].scheme", "unparseable")
    ]


def test_the_invoice_pipeline_is_the_same_pipeline_with_the_invoice_spec() -> None:
    pipeline = InvoicePipeline(FakeParser(), ScriptedProvider([]))

    assert isinstance(pipeline, ExtractionPipeline)
    assert pipeline.spec.doc_type == "invoice"
    assert PURCHASE_ORDER_SPEC.doc_type == "purchase_order"


def test_a_serial_number_left_in_the_product_cell_is_removed_from_the_value(
    raw_order_from_label: RawFromLabel,
) -> None:
    """The parser merges "1" and the product into one cell, and the model may copy both."""
    raw = raw_order_from_label(LABEL)
    first, second = raw["lines"][0]["product_name"], raw["lines"][1]["product_name"]
    names = (first["text"], second["text"])
    first["text"], second["text"] = f"1 {names[0]}", f"2 {names[1]}"

    order = extract(raw)

    assert [line.product_name.value for line in order.lines[:2]] == list(names)
    assert order.lines[0].product_name.raw == f"1 {names[0]}"  # what the model returned is kept


def test_a_leading_number_that_is_not_the_lines_own_position_is_kept(
    raw_order_from_label: RawFromLabel,
) -> None:
    raw = raw_order_from_label(LABEL)
    raw["lines"][0]["product_name"]["text"] = "5 Fluorouracil Injection"

    assert extract(raw).lines[0].product_name.value == "5 Fluorouracil Injection"


def printed(text: str | None) -> dict[str, Any]:
    return {"text": text, "block_ids": []}


def with_amounts(raw: dict[str, Any], amounts: list[str | None], total: str | None) -> None:
    for line, amount in zip(raw["lines"], amounts, strict=False):
        line["amount"] = printed(amount)
    raw["total"] = printed(total)


def test_each_lines_hsn_and_amount_and_the_order_total_are_read(
    raw_order_from_label: RawFromLabel,
) -> None:
    raw = raw_order_from_label(LABEL)
    raw["lines"][0]["hsn"] = printed(" 30049099 ")
    with_amounts(raw, ["1,200.00"], "2,242.00")

    order = extract(raw)

    assert order.lines[0].hsn.value == "30049099"
    assert order.lines[0].amount.value == Decimal("1200.00")
    assert order.total.value == Decimal("2242.00")
    assert order.lines[1].hsn.value is None and order.lines[1].amount.value is None


def test_an_order_read_before_hsn_amounts_and_total_still_reads(
    raw_order_from_label: RawFromLabel,
) -> None:
    """Stored replies and records of the first schema are read again (review, matching)."""
    raw = raw_order_from_label(LABEL)
    del raw["total"]
    for line in raw["lines"]:
        del line["hsn"], line["amount"]

    order = normalize_purchase_order(RawPurchaseOrder.model_validate(raw), NO_BLOCKS)
    stored = order.model_dump(mode="json")
    stored["schema_version"] = "purchase-order-1"
    del stored["total"]
    for line in stored["lines"]:
        del line["hsn"], line["amount"]
    old = PurchaseOrderExtraction.model_validate(stored)

    assert order.total.value is None and order.lines[0].amount.value is None
    assert old.schema_version == "purchase-order-1"
    assert old.total.value is None and old.lines[0].hsn.value is None


def total_check(raw: dict[str, Any]) -> Any:
    order = normalize_purchase_order(RawPurchaseOrder.model_validate(raw), NO_BLOCKS)
    (found,) = [r for r in order_rules(order) if r.rule_id == "order.total"]
    return found


def test_line_amounts_that_add_up_to_the_printed_total_pass(
    raw_order_from_label: RawFromLabel,
) -> None:
    raw = raw_order_from_label(LABEL)
    amounts = [f"{100 * (index + 1)}.50" for index in range(len(raw["lines"]))]
    total = sum((Decimal(a) for a in amounts), Decimal(0))
    with_amounts(raw, list(amounts), f"{total:,}")

    found = total_check(raw)

    assert (found.outcome, found.severity) == ("passed", "warning")


def test_line_amounts_that_do_not_add_up_to_the_total_are_a_warning(
    raw_order_from_label: RawFromLabel,
) -> None:
    raw = raw_order_from_label(LABEL)
    with_amounts(raw, ["100.00"] * len(raw["lines"]), "1.00")

    found = total_check(raw)

    assert (found.outcome, found.severity) == ("failed", "warning")
    assert found.paths[0] == "total"
    # A warning: the order is still accepted on its own checks.
    order = normalize_purchase_order(RawPurchaseOrder.model_validate(raw), NO_BLOCKS)
    assert all(r.severity == "warning" for r in order_rules(order) if r.outcome == "failed")


def test_without_every_amount_and_the_total_the_sum_is_not_checked(
    raw_order_from_label: RawFromLabel,
) -> None:
    raw = raw_order_from_label(LABEL)
    with_amounts(raw, ["100.00"], "100.00")  # the other lines print no amount

    assert total_check(raw).outcome == "not_evaluated"
    assert total_check(raw_order_from_label(LABEL)).outcome == "not_evaluated"
