"""The compact form a model replies in.

A `RawField` written as JSON (`{"text": "1,250.00", "block_ids": ["b12"]}`) costs several times
the tokens of its value, and output tokens are most of what a reading costs. The model is held
instead to a wire model in which every field is one string, `"1,250.00|b12"`: the value as
printed, a bar, and the ids of the blocks it was read from. The value comes first, as `text` came
before `block_ids`: a model that names the block before writing the value cites the cell beside
it more often. Validating the reply turns each string back into a `RawField`, so the reply
becomes the `Raw*` model and nothing after it changes.
"""

import re
from functools import cache
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, Field, WithJsonSchema, create_model
from pydantic.fields import FieldInfo

from docforge.extraction.schema import RawField

CELL_INSTRUCTION = """
Reply format: every field is one string - the value exactly as printed, a vertical bar, then
the ids of the blocks it was read from, e.g. "1,250.00|b12" or "Dolo 650 Tablet|b4 b5". This
holds for every field of every line item, however many lines there are: never a value without
its bar and block ids. A field that is not printed is null.
"""

# Repeated on each field of the schema, where the model looks when it writes the field.
CELL_FORMAT = 'Written as "<value as printed>|<block ids>", or null.'

_CELL = re.compile(r"(.*)\|\s*((?:b\d+[\s,]*)*)", re.DOTALL)  # the last bar separates
_BLOCK_ID = re.compile(r"b\d+")


def _not_printed() -> RawField:
    return RawField(text=None, block_ids=[])


def _read_cell(value: Any) -> Any:
    """A cell to the `RawField` it stands for. A field in the old form is passed through."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return _not_printed()  # models sometimes write "" for a field that is not printed
    if not isinstance(value, str):
        return value  # the old form, {text, block_ids}: validated as a RawField
    match = _CELL.fullmatch(value)
    if match is None:
        raise ValueError("expected '<value as printed>|<block ids>', e.g. '1,250.00|b12'")
    text = match[1].strip()
    if not text:
        return _not_printed()
    # A value without ids ("Dolo 650|") is kept, as the old form allowed: the trust layer
    # finds no citation and sends the document to a person, so nothing is accepted unseen.
    return RawField(text=text, block_ids=_BLOCK_ID.findall(match[2]))


Cell = Annotated[
    RawField,
    BeforeValidator(_read_cell),
    WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]}),
]


def _wire_type(annotation: Any) -> Any:
    if annotation is RawField:
        return Cell
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return wire_model(annotation)
    if get_origin(annotation) is list:
        (item,) = get_args(annotation)
        return list[_wire_type(item)]  # type: ignore[misc]
    return annotation


def _holds_cells(annotation: Any) -> bool:
    return annotation is RawField or (
        get_origin(annotation) is list and get_args(annotation) == (RawField,)
    )


def _description(info: FieldInfo) -> str | None:
    if not _holds_cells(info.annotation):
        return info.description
    return f"{info.description} {CELL_FORMAT}" if info.description else CELL_FORMAT


@cache
def wire_model(raw: type[BaseModel]) -> type[BaseModel]:
    """`raw` with every `RawField` written as one string; each such field says so."""
    fields: dict[str, Any] = {
        name: (_wire_type(info.annotation), Field(description=_description(info)))
        for name, info in raw.model_fields.items()
    }
    model: type[BaseModel] = create_model(f"Wire{raw.__name__}", __doc__=raw.__doc__, **fields)
    return model


def to_raw[R: BaseModel](raw: type[R], wire: BaseModel) -> R:
    """The `Raw*` reply that a validated wire reply stands for."""
    return raw.model_validate(wire.model_dump())
