"""Parse, extract, normalise: one invoice PDF to one typed record."""

from dataclasses import dataclass, replace

from pydantic import ValidationError

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTION, build_prompt
from docforge.extraction.schema import InvoiceExtraction, RawInvoice
from docforge.llm.base import LLMProvider, LLMRequest, LLMResponse
from docforge.parsing.base import DocumentTooLarge, ParsedDocument, Parser
from docforge.parsing.pdf import pdf_page_count

DEFAULT_MAX_PAGES = 20


class ExtractionError(Exception):
    """The model did not return output that fits the schema."""


@dataclass(frozen=True)
class PipelineResult:
    parsed: ParsedDocument
    extraction: InvoiceExtraction
    responses: tuple[LLMResponse, ...]  # one per model call, in order


def _describe(error: ValidationError) -> str:
    problems = error.errors(include_url=False, include_input=False)
    return "\n".join(
        f"- {'.'.join(str(part) for part in problem['loc']) or 'reply'}: {problem['msg']}"
        for problem in problems[:20]
    )


class InvoicePipeline:
    def __init__(
        self, parser: Parser, provider: LLMProvider, max_pages: int = DEFAULT_MAX_PAGES
    ) -> None:
        self.parser = parser
        self.provider = provider
        self.max_pages = max_pages

    def run(self, pdf: bytes) -> PipelineResult:
        """Raises `ParseError`, `LLMError` or `ExtractionError`."""
        pages = pdf_page_count(pdf)
        if pages > self.max_pages:
            raise DocumentTooLarge(f"document has {pages} pages; the limit is {self.max_pages}")
        return self.extract(self.parser.parse(pdf))

    def extract(self, parsed: ParsedDocument) -> PipelineResult:
        """Extraction from an already parsed document. One retry if the reply is malformed."""
        request = LLMRequest(
            system=SYSTEM_INSTRUCTION,
            prompt=build_prompt(parsed),
            schema=RawInvoice,
            prompt_version=PROMPT_VERSION,
        )
        first = self.provider.generate(request)
        try:
            raw = RawInvoice.model_validate_json(first.text)
            responses: tuple[LLMResponse, ...] = (first,)
        except ValidationError as error:
            retry = replace(
                request,
                prompt=f"{request.prompt}\n\nYour previous reply was not valid:\n"
                f"{_describe(error)}\nReply again with JSON that fits the schema.",
            )
            second = self.provider.generate(retry)
            responses = (first, second)
            try:
                raw = RawInvoice.model_validate_json(second.text)
            except ValidationError as second_error:
                raise ExtractionError(
                    "model reply did not fit the schema after one retry"
                ) from second_error
        return PipelineResult(
            parsed=parsed, extraction=normalize_invoice(raw, parsed), responses=responses
        )
