"""The compact reply format: each field one string, `"<block ids>|<printed text>"`.

The model is held to the wire model; code turns its reply back into the `Raw*` model, so
everything after the reply (normalising, rules, merging pages, corrections) is unchanged.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from docforge.extraction.coa import COA_SPEC, RawCoa
from docforge.extraction.pipeline import INVOICE_SPEC, ExtractionPipeline, InvoicePipeline
from docforge.extraction.prompt import PAGED_INSTRUCTION
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC, RawPurchaseOrder
from docforge.extraction.schema import RawInvoice, RawLine
from docforge.extraction.wire import CELL_INSTRUCTION, to_raw, wire_model
from fakes import FakeParser, ScriptedProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]

EMPTY_COA: dict[str, Any] = {
    **{name: None for name in RawCoa.model_fields if name != "tests"},
    "tests": [],
}


def as_wire(raw: Any) -> Any:
    """A raw reply (fields as {text, block_ids}) written in the compact form."""
    if isinstance(raw, dict) and set(raw) == {"text", "block_ids"}:
        if raw["text"] is None:
            return None
        return f"{' '.join(raw['block_ids'])}|{raw['text']}"
    if isinstance(raw, dict):
        return {key: as_wire(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return [as_wire(item) for item in raw]
    return raw


def fields(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict) and set(node) == {"text", "block_ids"}:
        return [node]
    if isinstance(node, dict):
        return [field for value in node.values() for field in fields(value)]
    if isinstance(node, list):
        return [field for item in node for field in fields(item)]
    return []


def coa_with(batch_no: Any) -> Any:
    return wire_model(RawCoa).model_validate({**EMPTY_COA, "batch_no": batch_no})


def test_every_field_is_one_string_in_the_schema_sent_to_the_model() -> None:
    schema = wire_model(RawInvoice).model_json_schema()

    assert "block_ids" not in json.dumps(schema)
    line = schema["$defs"]["WireRawLine"]["properties"]
    assert set(line) == set(RawLine.model_fields)
    assert line["batch_no"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_descriptions_are_kept_so_the_model_still_knows_each_field() -> None:
    line = wire_model(RawInvoice).model_json_schema()["$defs"]["WireRawLine"]["properties"]

    assert line["ptr"]["description"] == "Price to retailer; may be labelled Rate."


def test_the_wire_model_is_made_once_per_raw_model() -> None:
    assert wire_model(RawInvoice) is wire_model(RawInvoice)


@pytest.mark.parametrize(
    ("cell", "text", "block_ids"),
    [
        ("b3|Dolo 650", "Dolo 650", ["b3"]),
        ("b3 b4|Dolo 650", "Dolo 650", ["b3", "b4"]),
        ("b3, b4|Dolo 650", "Dolo 650", ["b3", "b4"]),
        (" b12 | 1,234.00 ", "1,234.00", ["b12"]),
        ("b7|10|1 scheme", "10|1 scheme", ["b7"]),  # only the first bar separates
        ("|Dolo 650", "Dolo 650", []),  # a value without a citation, as the old form allowed
        ("b3|", None, []),  # nothing printed after the bar is no value
        (None, None, []),
    ],
)
def test_a_cell_becomes_the_printed_text_and_its_blocks(
    cell: str | None, text: str | None, block_ids: list[str]
) -> None:
    raw = to_raw(RawCoa, coa_with(cell))

    assert raw.batch_no.text == text
    assert raw.batch_no.block_ids == block_ids


@pytest.mark.parametrize("cell", ["Dolo 650", "b3 Dolo 650", "x3|Dolo", "b3;b4|Dolo"])
def test_a_cell_without_its_bar_or_with_a_bad_block_id_is_refused(cell: str) -> None:
    with pytest.raises(ValidationError, match="batch_no"):
        coa_with(cell)


def test_the_old_form_of_a_field_is_still_read() -> None:
    old = {"text": "B123", "block_ids": ["b9"]}

    assert to_raw(RawCoa, coa_with(old)).batch_no.model_dump() == old


def test_a_reply_in_the_compact_form_reads_the_same_as_one_in_the_old_form(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    raw = raw_invoice_from_label(LABEL)
    old = InvoicePipeline(FakeParser(), ScriptedProvider([json.dumps(raw)])).run(PDF)
    new = InvoicePipeline(FakeParser(), ScriptedProvider([json.dumps(as_wire(raw))])).run(PDF)

    assert new.extraction == old.extraction
    assert new.raw == old.raw
    assert isinstance(new.raw, RawInvoice)


def test_the_compact_form_is_less_than_half_as_long(raw_invoice_from_label: RawFromLabel) -> None:
    raw = raw_invoice_from_label(LABEL)
    for field in fields(raw):
        field["block_ids"] = ["b12"] if field["text"] is not None else []

    assert len(json.dumps(as_wire(raw))) * 2 < len(json.dumps(raw))


def test_the_model_is_sent_the_wire_schema_and_told_the_cell_format(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    provider = ScriptedProvider([json.dumps(as_wire(raw_invoice_from_label(LABEL)))])

    InvoicePipeline(FakeParser(), provider).run(PDF)

    (request,) = provider.requests
    assert request.schema is wire_model(RawInvoice)
    assert CELL_INSTRUCTION in request.system
    assert "block ids" not in request.system.replace(CELL_INSTRUCTION, "")
    assert request.prompt_version == INVOICE_SPEC.prompt_version == "invoice-v2"


def test_the_paged_instruction_no_longer_asks_for_a_list_of_block_ids() -> None:
    assert "list of block ids" not in PAGED_INSTRUCTION


def test_a_malformed_cell_is_retried_with_its_path(raw_invoice_from_label: RawFromLabel) -> None:
    good = as_wire(raw_invoice_from_label(LABEL))
    bad = json.loads(json.dumps(good))
    bad["lines"][0]["batch_no"] = "no bar here"
    provider = ScriptedProvider([json.dumps(bad), json.dumps(good)])

    result = InvoicePipeline(FakeParser(), provider).run(PDF)

    assert len(result.responses) == 2
    assert "lines.0.batch_no" in provider.requests[1].prompt


@pytest.mark.parametrize(
    ("spec", "raw_model", "version"),
    [
        (PURCHASE_ORDER_SPEC, RawPurchaseOrder, "purchase-order-v3"),
        (COA_SPEC, RawCoa, "coa-v2"),
    ],
)
def test_every_document_type_replies_in_the_compact_form(
    spec: Any, raw_model: Any, version: str
) -> None:
    assert spec.wire_schema is wire_model(raw_model)
    assert CELL_INSTRUCTION in spec.system_instruction
    assert spec.prompt_version == version


def test_a_document_type_without_a_wire_schema_is_asked_in_its_raw_form(
    raw_invoice_from_label: RawFromLabel,
) -> None:
    spec = replace(INVOICE_SPEC, wire_schema=None)
    provider = ScriptedProvider([json.dumps(raw_invoice_from_label(LABEL))])

    ExtractionPipeline(FakeParser(), provider, spec).run(PDF)

    assert provider.requests[0].schema is RawInvoice
