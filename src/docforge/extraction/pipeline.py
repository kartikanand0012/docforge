"""Parse, extract, normalise: one PDF to one typed record.

The pipeline is the same for every document type. What differs is the `DocumentSpec`:
the shape the model must reply in, the instruction it is given, and the code that turns
its printed strings into typed values.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTION, build_prompt
from docforge.extraction.schema import InvoiceExtraction, RawInvoice
from docforge.llm.base import LLMProvider, LLMRequest, LLMResponse
from docforge.parsing.base import DocumentTooLarge, NoTextLayer, ParsedDocument, Parser
from docforge.parsing.pdf import pdf_page_count

DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_PROMPT_CHARS = 200_000  # far above any invoice; bounds model cost per request


class ExtractionError(Exception):
    """The model did not return output that fits the schema."""

    def __init__(self, message: str, responses: tuple[LLMResponse, ...] = ()) -> None:
        super().__init__(message)
        self.responses = responses  # the calls that were made, for usage accounting


@dataclass(frozen=True)
class DocumentSpec[E: BaseModel]:
    """Everything that is specific to one document type."""

    doc_type: str
    raw_schema: type[BaseModel]  # what the model returns: printed strings and block ids
    system_instruction: str
    prompt_version: str  # change whenever the instruction changes
    schema_version: str
    normalize: Callable[[Any, ParsedDocument], E]  # printed strings to typed values, in code


@dataclass(frozen=True)
class PipelineResult[E: BaseModel]:
    parsed: ParsedDocument
    extraction: E
    responses: tuple[LLMResponse, ...]  # one per model call, in order
    prompt_version: str
    schema_version: str


def _describe(error: ValidationError) -> str:
    problems = error.errors(include_url=False, include_input=False)
    return "\n".join(
        f"- {'.'.join(str(part) for part in problem['loc']) or 'reply'}: {problem['msg']}"
        for problem in problems[:20]
    )


class ExtractionPipeline[E: BaseModel]:
    def __init__(
        self,
        parser: Parser,
        provider: LLMProvider,
        spec: DocumentSpec[E],
        max_pages: int = DEFAULT_MAX_PAGES,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    ) -> None:
        self.parser = parser
        self.provider = provider
        self.spec = spec
        self.max_pages = max_pages
        self.max_prompt_chars = max_prompt_chars

    def run(self, pdf: bytes) -> PipelineResult[E]:
        """Raises `ParseError`, `LLMError` or `ExtractionError`."""
        pages = pdf_page_count(pdf)
        if pages > self.max_pages:
            raise DocumentTooLarge(f"document has {pages} pages; the limit is {self.max_pages}")
        return self.extract(self.parser.parse(pdf))

    def extract(self, parsed: ParsedDocument) -> PipelineResult[E]:
        """Extraction from an already parsed document. One retry if the reply is malformed.

        Raises `NoTextLayer`, `DocumentTooLarge`, `LLMError` or `ExtractionError`.
        """
        if not parsed.blocks:
            # Without this the model is sent an empty document and returns an all-null record.
            raise NoTextLayer("the PDF has no extractable text")
        prompt = build_prompt(parsed)
        if len(prompt) > self.max_prompt_chars:
            raise DocumentTooLarge(
                f"document text is {len(prompt)} characters; the limit is {self.max_prompt_chars}"
            )
        spec = self.spec
        request = LLMRequest(
            system=spec.system_instruction,
            prompt=prompt,
            schema=spec.raw_schema,
            prompt_version=spec.prompt_version,
        )
        first = self.provider.generate(request)
        try:
            raw = spec.raw_schema.model_validate_json(first.text)
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
                raw = spec.raw_schema.model_validate_json(second.text)
            except ValidationError as second_error:
                raise ExtractionError(
                    "model reply did not fit the schema after one retry", responses
                ) from second_error
        return PipelineResult(
            parsed=parsed,
            extraction=spec.normalize(raw, parsed),
            responses=responses,
            prompt_version=spec.prompt_version,
            schema_version=spec.schema_version,
        )


INVOICE_SPEC = DocumentSpec(
    doc_type="invoice",
    raw_schema=RawInvoice,
    system_instruction=SYSTEM_INSTRUCTION,
    prompt_version=PROMPT_VERSION,
    schema_version="invoice-1",
    normalize=normalize_invoice,
)


class InvoicePipeline(ExtractionPipeline[InvoiceExtraction]):
    """The pipeline with the invoice spec."""

    def __init__(
        self,
        parser: Parser,
        provider: LLMProvider,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_prompt_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    ) -> None:
        super().__init__(parser, provider, INVOICE_SPEC, max_pages, max_prompt_chars)
