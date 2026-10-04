"""General documents: contracts, SOPs, manuals, slides. Read and indexed for search and chat,
with no field extraction, so no model call and nothing for a person to check.

What is recorded is what the parse shows: a title (the first line), pages and words.
"""

from pydantic import BaseModel, ConfigDict

from docforge.extraction.pipeline import DEFAULT_MAX_PAGES, PipelineResult
from docforge.parsing.base import DocumentTooLarge, NoTextLayer, ParsedDocument, Parser
from docforge.parsing.pdf import pdf_page_count
from docforge.telemetry import traced
from docforge.trust.assess import Assessment

GENERAL_SCHEMA_VERSION = "general-1"
_TITLE_LIMIT = 120


class GeneralExtraction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str | None
    pages: int
    words: int


def _title(parsed: ParsedDocument) -> str | None:
    first = next((b.text for b in parsed.blocks if b.kind == "text" and b.text.strip()), None)
    if first is None:
        return None
    first = " ".join(first.split())
    return first if len(first) <= _TITLE_LIMIT else first[: _TITLE_LIMIT - 1].rstrip() + "…"


class GeneralPipeline:
    def __init__(self, parser: Parser, max_pages: int = DEFAULT_MAX_PAGES) -> None:
        self.parser = parser
        self.max_pages = max_pages

    def run(self, pdf: bytes) -> PipelineResult[GeneralExtraction]:
        """Raises `ParseError` (and its kinds `NoTextLayer` and `DocumentTooLarge`)."""
        pages = pdf_page_count(pdf)
        if pages > self.max_pages:
            raise DocumentTooLarge(f"document has {pages} pages; the limit is {self.max_pages}")
        with traced("pipeline.parse") as span:
            parsed = self.parser.parse(pdf)
            span.set_attribute("docforge.pages", len(parsed.pages))
            span.set_attribute("docforge.blocks", len(parsed.blocks))
        if not parsed.blocks:
            raise NoTextLayer("the document has no text that could be read")
        extraction = GeneralExtraction(
            title=_title(parsed),
            pages=len(parsed.pages),
            words=sum(len(block.text.split()) for block in parsed.blocks),
        )
        return PipelineResult(
            parsed=parsed,
            extraction=extraction,
            responses=(),
            raw=extraction,
            prompt_version="none",
            schema_version=GENERAL_SCHEMA_VERSION,
            # Nothing was extracted, so nothing can be wrong or held back.
            assessment=Assessment(decision="accept", reasons=(), fields=(), rules=(), issues=()),
        )
