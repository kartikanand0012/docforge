"""The reply schema Claude and OpenAI are held to.

Their strict structured-output modes accept a narrower JSON Schema than Gemini: no references,
every object closed (`additionalProperties: false`) and every property required. This turns a
pydantic model's schema into that form. The model itself still validates every reply: a field
with a default becomes required, and the provider sends the default.

Gemini is still sent the pydantic class, unchanged.
"""

import copy
from typing import Any

from pydantic import BaseModel

# Keywords at least one provider rejects in strict mode. Ours use none; refusing them keeps
# it so, rather than letting a provider fail the request in production.
_REFUSED = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "patternProperties",
    }
)
_DROPPED = frozenset({"title", "default"})


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model`'s JSON Schema, self-contained, closed and with every property required."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})
    strict: dict[str, Any] = _strict(schema, definitions, ())
    return strict


def _strict(node: Any, definitions: dict[str, Any], seen: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_strict(item, definitions, seen) for item in node]
    if not isinstance(node, dict):
        return node
    refused = _REFUSED & node.keys()
    if refused:
        raise ValueError(f"strict structured output does not allow {', '.join(sorted(refused))}")
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            raise ValueError(f"a recursive schema ({name}) cannot be made self-contained")
        # The reference's own description (beside it) wins over the definition's.
        beside = {key: value for key, value in node.items() if key != "$ref"}
        merged = {**copy.deepcopy(definitions[name]), **beside}
        return _strict(merged, definitions, (*seen, name))
    out = {
        key: _strict(value, definitions, seen)
        for key, value in node.items()
        if key not in _DROPPED and key != "$defs"
    }
    if "anyOf" in out:
        out = _nullable(out)
    if out.get("type") == "object" or "properties" in out:
        out["type"] = "object"
        out.setdefault("properties", {})
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    """`X | None` of a simple type as `type: [X, "null"]`; anything else stays an `anyOf`."""
    options = node["anyOf"]
    simple = [o for o in options if set(o) == {"type"} and isinstance(o["type"], str)]
    if len(simple) == len(options) == 2 and {"type": "null"} in options:
        (kept,) = [o["type"] for o in simple if o["type"] != "null"]
        rest = {key: value for key, value in node.items() if key != "anyOf"}
        return {**rest, "type": [kept, "null"]}
    return node
