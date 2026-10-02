"""Stand-ins for the parser and the model, shared by the pipeline and API tests."""

from collections.abc import Sequence

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
