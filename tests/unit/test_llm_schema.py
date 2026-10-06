"""The reply schema Claude and OpenAI are held to: strict, self-contained, every field present.

Their strict modes accept a narrower JSON Schema than Gemini: no references, every object
closed (`additionalProperties: false`) and every property required. The same pydantic models
still validate every reply; only what the provider is sent differs.
"""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from docforge.chat.service import RawAnswer
from docforge.extraction.coa import RawCoa
from docforge.extraction.purchase_order import RawPurchaseOrder
from docforge.extraction.schema import RawInvoice
from docforge.llm.schema import strict_json_schema

REPLIES = [RawInvoice, RawPurchaseOrder, RawCoa, RawAnswer]


def walk(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        found.append(schema)
        for value in schema.values():
            found += walk(value)
    elif isinstance(schema, list):
        for value in schema:
            found += walk(value)
    return found


@pytest.mark.parametrize("model", REPLIES, ids=lambda m: m.__name__)
def test_every_reply_schema_is_self_contained_closed_and_complete(model: type[BaseModel]) -> None:
    strict = strict_json_schema(model)
    nodes = walk(strict)
    assert not any("$ref" in n or "$defs" in n for n in nodes)
    assert not any("title" in n or "default" in n for n in nodes)
    objects = [n for n in nodes if n.get("type") == "object"]
    assert objects and all(n["additionalProperties"] is False for n in objects)
    assert all(sorted(n["required"]) == sorted(n["properties"]) for n in objects)


def test_a_field_with_a_default_becomes_required_and_descriptions_stay() -> None:
    strict = strict_json_schema(RawAnswer)
    assert "missing" in strict["required"]
    assert strict["properties"]["missing"]["description"].startswith("If unanswerable")


def test_a_value_that_may_be_missing_is_a_type_or_null() -> None:
    field = strict_json_schema(RawInvoice)["properties"]["invoice_no"]
    assert field["properties"]["text"]["type"] == ["string", "null"]
    assert field["description"]  # the description beside the reference is kept


class Node(BaseModel):
    name: str
    children: list["Node"]


class Bounded(BaseModel):
    code: str = Field(min_length=3)


def test_a_recursive_schema_or_a_keyword_strict_modes_reject_is_refused() -> None:
    with pytest.raises(ValueError, match="recursive"):
        strict_json_schema(Node)
    with pytest.raises(ValueError, match="minLength"):
        strict_json_schema(Bounded)
