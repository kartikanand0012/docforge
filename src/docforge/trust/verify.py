"""Check each extracted value against the text of the blocks it cites.

The model copies text and points at blocks. This is the check that it did: the printed
string must appear in the cited blocks. A value that does not is flagged for review and is
never corrected here.
"""

import re
import unicodedata
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict

from docforge.extraction.schema import Extracted
from docforge.parsing.base import ParsedDocument

Status = Literal["verified", "not_in_cited_blocks", "no_citation"]


class Box(BaseModel):
    """Where a cited block sits. PDF points, origin at the bottom-left of the page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class FieldCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    status: Status
    boxes: tuple[Box, ...]  # the cited blocks, whether or not the value was found in them
    found_in: tuple[str, ...]  # other blocks that do contain the value, when not verified


def extracted_fields(model: BaseModel, prefix: str = "") -> Iterator[tuple[str, Extracted[object]]]:
    """Every `Extracted` field in an extraction, with its path (`lines[0].qty`)."""
    for name in type(model).model_fields:
        value = getattr(model, name)
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, Extracted):
            yield path, value
        elif isinstance(value, BaseModel):
            yield from extracted_fields(value, path)
        elif isinstance(value, tuple):
            for index, item in enumerate(value):
                if isinstance(item, Extracted):
                    yield f"{path}[{index}]", item
                elif isinstance(item, BaseModel):
                    yield from extracted_fields(item, f"{path}[{index}]")


def _normalise(text: str) -> str:
    """The same folding the prompt applies, so the comparison is like for like."""
    return " ".join(unicodedata.normalize("NFKC", text).split())


_NUMBER = re.compile(r"-?[\d,]*\d(?:\.\d+)?%?", re.ASCII)
_SHORT = 3  # a number this short proves nothing unless it is all the cited block says


def _contains(haystack: str, needle: str) -> bool:
    """True if `needle` occurs in `haystack` as a whole token, not inside a longer one.

    "20" is not found in "200", "166.40" not in "1,166.40", "86.24" not in "86.245", "05/29"
    not in "05/29/2028", and a
    number is not found in its negative ("-5") or bracketed ("(5.00)") form.
    """
    if not needle:
        return False
    before = r"(?<![0-9A-Za-z])(?<![0-9][.,/-])"
    if needle[0].isdigit():
        before += r"(?<![-+(])"
    pattern = before + re.escape(needle) + r"(?![0-9A-Za-z])(?![.,/-][0-9])"
    return re.search(pattern, haystack) is not None


def _supported(needle: str, cited: list[str]) -> bool:
    """Do the cited blocks support the value?

    A short number must be the whole text of a cited block, or one of the numbers in a
    block that holds only numbers. Any other value may sit inside one
    cited block. Only text, such as an address, may run across several cited blocks: a
    number assembled from pieces of two blocks is not on the page.
    """
    if not needle:
        return False
    numeric = _NUMBER.fullmatch(needle) is not None
    if numeric and len(needle) <= _SHORT:
        # Or one of several numbers in a block that holds nothing else: the parser
        # sometimes merges two numeric cells. Which of them it is, the rules must check.
        return any(
            needle in tokens and all(_NUMBER.fullmatch(token) for token in tokens)
            for tokens in (text.split() for text in cited)
        )
    if any(_contains(text, needle) for text in cited):
        return True
    return not numeric and len(cited) > 1 and _contains(" ".join(cited), needle)


def verify_extraction(extraction: BaseModel, parsed: ParsedDocument) -> tuple[FieldCheck, ...]:
    """One check per field that has a value. Fields the model left null are not checked."""
    texts = {block.id: _normalise(block.text) for block in parsed.blocks}
    blocks = {block.id: block for block in parsed.blocks}
    checks: list[FieldCheck] = []
    for path, field in extracted_fields(extraction):
        if field.raw is None:
            continue
        needle = _normalise(field.raw)
        cited = [block_id for block_id in field.block_ids if block_id in texts]
        if not cited:
            status: Status = "no_citation"
        elif _supported(needle, [texts[block_id] for block_id in cited]):
            status = "verified"
        else:
            status = "not_in_cited_blocks"
        checks.append(
            FieldCheck(
                path=path,
                status=status,
                boxes=tuple(Box(page=blocks[b].page, **blocks[b].bbox.model_dump()) for b in cited),
                found_in=()
                if status == "verified"
                else tuple(b for b, text in texts.items() if _contains(text, needle)),
            )
        )
    return tuple(checks)
