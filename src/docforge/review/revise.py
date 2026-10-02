"""Corrections to an extraction, applied to the model's reply and read again.

A correction replaces the printed text of one field. The corrected reply goes through the same
normaliser and the same checks as the model's, so a reviewer's typing mistake is caught the
same way a model's would be. A corrected (or re-entered) value counts as confirmed: it no
longer needs to be found in the text it cites.
"""

import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from docforge.extraction.pipeline import DocumentSpec
from docforge.parsing.base import ParsedDocument
from docforge.trust.assess import Assessment, assess

_SEGMENT = re.compile(r"([A-Za-z_]\w*)(?:\[(\d+)\])?")
# Fields read from another field's text: confirming the one confirms the other.
_DERIVED = {"place_of_supply": ("place_of_supply_code",)}


class Correction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str  # e.g. "lines[0].qty", as in the extraction
    text: str | None  # the value as printed; None when it is not printed at all


class Reassessed(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    raw: BaseModel
    extraction: BaseModel
    assessment: Assessment


def _field(data: dict[str, Any], path: str) -> dict[str, Any]:
    """The `{"text", "block_ids"}` dict at `path` in a dumped reply."""
    node: Any = data
    for part in path.split("."):
        match = _SEGMENT.fullmatch(part)
        if match is None or not isinstance(node, dict) or match[1] not in node:
            raise ValueError(f"{path} is not a field of this document")
        node = node[match[1]]
        if match[2] is not None:
            index = int(match[2])
            if not isinstance(node, list) or index >= len(node):
                raise ValueError(f"{path} is not a field of this document")
            node = node[index]
    if not (isinstance(node, dict) and set(node) == {"text", "block_ids"}):
        raise ValueError(f"{path} is not a field of this document")
    return node


def apply_corrections[R: BaseModel](raw: R, corrections: Sequence[Correction]) -> R:
    """`raw` with each correction applied in order. Raises `ValueError` for a bad path."""
    data = raw.model_dump()
    for correction in corrections:
        _field(data, correction.path)["text"] = correction.text
    return type(raw).model_validate(data)


def reassess(
    spec: DocumentSpec[Any],
    raw: BaseModel,
    parsed: ParsedDocument,
    corrections: Sequence[Correction],
) -> Reassessed:
    """The record and its assessment after `corrections`."""
    corrected = apply_corrections(raw, corrections)
    extraction = spec.normalize(corrected, parsed)
    confirmed = {c.path for c in corrections}
    confirmed |= {derived for path in confirmed for derived in _DERIVED.get(path, ())}
    assessment = assess(extraction, parsed, spec.rules, frozenset(confirmed))
    return Reassessed(raw=corrected, extraction=extraction, assessment=assessment)
