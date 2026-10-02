"""The purchase-order document type, through the same pipeline as invoices."""

import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC, PurchaseOrderExtraction
from fakes import FakeParser, ScriptedProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "purchase_order.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


def extract(raw: dict[str, Any]) -> PurchaseOrderExtraction:
    provider = ScriptedProvider([json.dumps(raw)])
    return ExtractionPipeline(FakeParser(), provider, PURCHASE_ORDER_SPEC).run(PDF).extraction


def test_a_purchase_order_becomes_a_typed_record(raw_order_from_label: RawFromLabel) -> None:
    order = extract(raw_order_from_label(LABEL))
    truth = LABEL["purchase_order"]

    assert order.schema_version == "purchase-order-1"
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


def test_the_run_records_which_prompt_and_schema_were_used(
    raw_order_from_label: RawFromLabel,
) -> None:
    provider = ScriptedProvider([json.dumps(raw_order_from_label(LABEL))])

    result = ExtractionPipeline(FakeParser(), provider, PURCHASE_ORDER_SPEC).run(PDF)

    assert result.prompt_version == "purchase-order-v1"
    assert result.schema_version == "purchase-order-1"
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
