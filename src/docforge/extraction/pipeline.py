"""Parse, extract, normalise: one PDF to one typed record.

The pipeline is the same for every document type. What differs is the `DocumentSpec`:
the shape the model must reply in, the instruction it is given, and the code that turns
its printed strings into typed values.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from docforge.extraction.normalize import normalize_invoice
from docforge.extraction.prompt import (
    PAGED_INSTRUCTION,
    PAGED_VERSION,
    PROMPT_VERSION,
    SYSTEM_INSTRUCTION,
    build_prompt,
)
from docforge.extraction.schema import InvoiceExtraction, Issue, RawInvoice
from docforge.llm.base import LLMProvider, LLMRequest, LLMResponse
from docforge.parsing.base import DocumentTooLarge, NoTextLayer, ParsedDocument, Parser
from docforge.parsing.pdf import pdf_page_count
from docforge.telemetry import tracer
from docforge.trust.assess import Assessment, assess
from docforge.trust.invoice_rules import INVOICE_RULES
from docforge.trust.rules import RuleResult

DEFAULT_MAX_PAGES = 20
_MAX_PAGE_RETRIES = 2  # malformed replies retried per multi-page document
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
    rules: tuple[Callable[[E], Iterable[RuleResult]], ...] = ()  # deterministic checks


@dataclass(frozen=True)
class PipelineResult[E: BaseModel]:
    parsed: ParsedDocument
    extraction: E
    responses: tuple[LLMResponse, ...]  # one per model call, in order
    raw: BaseModel  # what the model returned, merged across pages; corrections apply to it
    prompt_version: str
    schema_version: str
    assessment: Assessment  # what was checked and whether a person must look


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
        with tracer.start_as_current_span("pipeline.parse") as span:
            parsed = self.parser.parse(pdf)
            span.set_attribute("docforge.pages", len(parsed.pages))
            span.set_attribute("docforge.blocks", len(parsed.blocks))
        return self.extract(parsed)

    def extract(self, parsed: ParsedDocument) -> PipelineResult[E]:
        """Extraction from an already parsed document. One retry if the reply is malformed.

        Raises `NoTextLayer`, `DocumentTooLarge`, `LLMError` or `ExtractionError`.
        """
        with tracer.start_as_current_span("pipeline.extract") as span:
            span.set_attribute("docforge.doc_type", self.spec.doc_type)
            return self._extract(parsed)

    def _extract(self, parsed: ParsedDocument) -> PipelineResult[E]:
        if not parsed.blocks:
            # Without this the model is sent an empty document and returns an all-null record.
            raise NoTextLayer("the PDF has no extractable text")
        prompt = build_prompt(parsed)
        if len(prompt) > self.max_prompt_chars:
            raise DocumentTooLarge(
                f"document text is {len(prompt)} characters; the limit is {self.max_prompt_chars}"
            )
        spec = self.spec
        if len(parsed.pages) <= 1:
            request = LLMRequest(
                system=spec.system_instruction,
                prompt=prompt,
                schema=spec.raw_schema,
                prompt_version=spec.prompt_version,
            )
            raw, responses = self._ask(request, ())
            prompt_version = spec.prompt_version
            conflicts: tuple[str, ...] = ()
        else:
            raw, responses, prompt_version, conflicts = self._ask_by_page(parsed)
        extraction = spec.normalize(raw, parsed)
        if conflicts:
            disagreements = tuple(
                Issue(
                    path=path,
                    code="conflicting_pages",
                    message="pages of the document give different values; the first was kept",
                )
                for path in conflicts
            )
            issues = (*getattr(extraction, "issues", ()), *disagreements)
            extraction = extraction.model_copy(update={"issues": issues})
        with tracer.start_as_current_span("pipeline.assess") as span:
            assessment = assess(extraction, parsed, spec.rules)
            span.set_attribute("docforge.decision", assessment.decision)
        return PipelineResult(
            parsed=parsed,
            extraction=extraction,
            responses=responses,
            raw=raw,
            prompt_version=prompt_version,
            schema_version=spec.schema_version,
            assessment=assessment,
        )

    def _generate(self, request: LLMRequest) -> LLMResponse:
        with tracer.start_as_current_span("llm.generate") as span:
            span.set_attribute("docforge.prompt_version", request.prompt_version)
            response = self.provider.generate(request)
            span.set_attribute("gen_ai.system", response.provider)
            span.set_attribute("gen_ai.request.model", response.model)
            span.set_attribute("gen_ai.usage.input_tokens", response.input_tokens or 0)
            span.set_attribute("gen_ai.usage.output_tokens", response.output_tokens or 0)
            span.set_attribute("docforge.thinking_tokens", response.thinking_tokens or 0)
            span.set_attribute("docforge.latency_ms", response.latency_ms)
            return response

    def _ask(
        self, request: LLMRequest, earlier: tuple[LLMResponse, ...], may_retry: bool = True
    ) -> tuple[BaseModel, tuple[LLMResponse, ...]]:
        """One request, retried once with the validation errors if the reply is malformed.

        Returns the reply and every call made so far, `earlier` included.
        """
        schema = self.spec.raw_schema
        first = self._generate(request)
        try:
            return schema.model_validate_json(first.text), (*earlier, first)
        except ValidationError as error:
            if not may_retry:
                raise ExtractionError(
                    "too many malformed replies for one document", (*earlier, first)
                ) from error
            retry = replace(
                request,
                prompt=f"{request.prompt}\n\nYour previous reply was not valid:\n"
                f"{_describe(error)}\nReply again with JSON that fits the schema.",
            )
        second = self._generate(retry)
        responses = (*earlier, first, second)
        try:
            return schema.model_validate_json(second.text), responses
        except ValidationError as second_error:
            raise ExtractionError(
                "model reply did not fit the schema after one retry", responses
            ) from second_error

    def _ask_by_page(
        self, parsed: ParsedDocument
    ) -> tuple[BaseModel, tuple[LLMResponse, ...], str, tuple[str, ...]]:
        """A request per page that has text, merged. Keeps each reply short enough to finish
        and each page's rows next to their own header.
        """
        spec = self.spec
        prompt_version = f"{spec.prompt_version}+{PAGED_VERSION}"
        replies: list[BaseModel] = []
        responses: tuple[LLMResponse, ...] = ()
        for asked, page in enumerate(sorted({block.page for block in parsed.blocks})):
            # Retries are counted for the document, so a bad run cannot double its cost.
            retried = len(responses) - asked
            request = LLMRequest(
                system=spec.system_instruction + PAGED_INSTRUCTION,
                prompt=build_prompt(parsed, page),
                schema=spec.raw_schema,
                prompt_version=prompt_version,
            )
            reply, responses = self._ask(request, responses, retried < _MAX_PAGE_RETRIES)
            replies.append(reply)
        merged, conflicts = merge_pages(spec.raw_schema, replies)
        return merged, responses, prompt_version, conflicts


def merge_pages(
    schema: type[BaseModel], pages: list[BaseModel]
) -> tuple[BaseModel, tuple[str, ...]]:
    """One reply from the replies for each page, and the paths where pages disagreed.

    A field is taken from the first page that prints it, so a header repeated on a later
    page does not replace the first; if a later page prints a different value, the path is
    reported. Line items are joined in page order. A list of plain fields (licence numbers)
    is the union of the pages' lists.
    """
    conflicts: list[str] = []

    def is_field(value: object) -> bool:
        return isinstance(value, dict) and set(value) == {"text", "block_ids"}

    def merge(values: list[Any], path: str) -> Any:
        first = values[0]
        if is_field(first):
            printed = [value for value in values if value["text"] is not None]
            if len({value["text"] for value in printed}) > 1:
                conflicts.append(path)
            return printed[0] if printed else first
        if isinstance(first, dict):
            return {
                key: merge([value[key] for value in values], f"{path}.{key}" if path else key)
                for key in first
            }
        if isinstance(first, list):
            filled = [value for value in values if value]
            if filled and is_field(filled[0][0]):
                # Each printed value once, in the order first seen.
                seen: dict[str | None, Any] = {}
                for item in (item for value in filled for item in value):
                    seen.setdefault(item["text"], item)
                return list(seen.values())
            return [item for value in values for item in value]
        return first

    merged = schema.model_validate(merge([page.model_dump() for page in pages], ""))
    return merged, tuple(conflicts)


INVOICE_SPEC = DocumentSpec(
    doc_type="invoice",
    raw_schema=RawInvoice,
    system_instruction=SYSTEM_INSTRUCTION,
    prompt_version=PROMPT_VERSION,
    schema_version="invoice-1",
    normalize=normalize_invoice,
    rules=INVOICE_RULES,
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
