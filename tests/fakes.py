"""Stand-ins for the parser and the model, shared by the pipeline and API tests."""

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from docforge.llm.base import LLMRequest, LLMResponse
from docforge.parsing.base import BBox, Block, Page, ParsedDocument

PARSED = ParsedDocument(
    parser="fake",
    parser_version="0",
    pages=(Page(number=1, width=842, height=595),),
    blocks=tuple(
        Block(id=f"b{n}", kind="text", text=f"text {n}", page=1, bbox=BBox(x0=0, y0=0, x1=1, y1=1))
        for n in range(1, 4)
    ),
)


class FakeParser:
    name = "fake"
    version = "0"

    def __init__(self) -> None:
        self.calls = 0

    def parse(self, pdf: bytes) -> ParsedDocument:
        self.calls += 1
        return PARSED


class ScriptedProvider:
    """Replies with the given texts in order; an exception in the list is raised instead."""

    name = "fake"
    model = "fake-1"

    def __init__(self, replies: Sequence[str | BaseException]) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return LLMResponse(
            text=reply,
            provider=self.name,
            model=self.model,
            input_tokens=100,
            output_tokens=50,
            latency_ms=1.0,
        )


_SEGMENT = re.compile(r"(\w+)|\[(\d+)\]")


def raw_field(raw: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The raw field at a label path such as `lines[0].qty`, or None if the reply has none."""
    node: Any = raw
    for name, index in _SEGMENT.findall(path):
        try:
            node = node[name] if name else node[int(index)]
        except (KeyError, IndexError):
            return None
    return node if isinstance(node, dict) else None


def cited(label: dict[str, Any], kind: str, raw: dict[str, Any]) -> ParsedDocument:
    """A parse with one block on each of the label's boxes; `raw` is made to cite them.

    Together they are what a perfect parser and a perfect model would produce.
    """
    document = label["documents"][kind]
    blocks = []
    for number, box in enumerate(document["boxes"], start=1):
        blocks.append(
            Block(
                id=f"b{number}",
                kind="text",
                text=box["text"],
                page=box["page"],
                bbox=BBox(x0=box["x0"], y0=box["y0"], x1=box["x1"], y1=box["y1"]),
            )
        )
        field = raw_field(raw, box["path"])
        if field is not None:
            field["block_ids"] = [f"b{number}"]
    page = Page(number=1, width=document["page_width"], height=document["page_height"])
    return ParsedDocument(parser="fake", parser_version="0", pages=(page,), blocks=tuple(blocks))


def reprint(parsed: ParsedDocument, raw: dict[str, Any], path: str, text: str) -> ParsedDocument:
    """Change what the document prints for one field: both the block and the model's copy."""
    field = raw_field(raw, path)
    assert field is not None, path
    (block_id,) = field["block_ids"]
    field["text"] = text
    blocks = tuple(
        block.model_copy(update={"text": text}) if block.id == block_id else block
        for block in parsed.blocks
    )
    return parsed.model_copy(update={"blocks": blocks})


class MappedParser:
    """Returns a prepared parse for each known file."""

    name = "fake"
    version = "0"

    def __init__(self) -> None:
        self._parses: dict[str, ParsedDocument] = {}

    def add(self, pdf: bytes, parsed: ParsedDocument) -> None:
        self._parses[hashlib.sha256(pdf).hexdigest()] = parsed

    def parse(self, pdf: bytes) -> ParsedDocument:
        return self._parses[hashlib.sha256(pdf).hexdigest()]
