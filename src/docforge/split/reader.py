"""Reading a multi-page invoice PDF once: the proposal to split it, or its extraction.

The worker hands a PDF of two pages or more here instead of to the invoice pipeline. It is
parsed once; the parse is examined for invoices (`detect.propose`); with one invoice (or none
found) the same parse goes on to the pipeline's extraction, so such a file is read exactly as
before, parsed once and with the same model calls.

A file longer than one invoice may be (`max_pages`) is accepted up to `max_bundle_pages`
pages as a possible bundle. If it holds one invoice after all, it fails with the page limit's
own message, as it would have at upload. If the parser cannot read it within its limits, the
splitter does not fail it: it proposes all pages as one invoice for a person to divide, with
the reason "could not detect automatically" (`detect.undetected`).
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from docforge.extraction.pipeline import ExtractionPipeline, PipelineResult
from docforge.parsing.base import DocumentTooLarge, ParsedDocument, ParserLimitExceeded
from docforge.split.detect import Proposal, propose, undetected
from docforge.trust.rules import RuleResult

logger = logging.getLogger(__name__)

ExtraRules = Callable[[ParsedDocument], Sequence[Callable[[Any], Iterable[RuleResult]]]]


class Splitter:
    def __init__(self, pipeline: ExtractionPipeline[Any], *, max_bundle_pages: int = 100) -> None:
        self._pipeline = pipeline
        self.max_pages = pipeline.max_pages  # one invoice's
        self.max_bundle_pages = max(max_bundle_pages, pipeline.max_pages)

    def read(
        self, pdf: bytes, pages: int, extra_rules_for: ExtraRules
    ) -> PipelineResult[Any] | Proposal:
        """`pages` is the PDF's page count (two or more). Raises what the pipeline raises."""
        if pages > self.max_bundle_pages:
            raise DocumentTooLarge(
                f"document has {pages} pages; the limit is {self.max_bundle_pages}"
            )
        try:
            parsed = self._pipeline.parser.parse(pdf)
        except (ParserLimitExceeded, DocumentTooLarge):
            if pages > self.max_pages:  # too long to be read as one invoice anyway
                logger.warning("a %s-page file could not be parsed to look for invoices", pages)
                return undetected(pages)
            raise
        try:
            proposal = propose(parsed)
        except Exception:  # never an error: a person divides the pages instead
            logger.exception("looking for invoices in a %s-page file failed", pages)
            return undetected(pages)
        if proposal is not None:
            return proposal if proposal.page_count == pages else undetected(pages)
        if pages > self.max_pages:
            raise DocumentTooLarge(f"document has {pages} pages; the limit is {self.max_pages}")
        return self._pipeline.extract(parsed, extra_rules=extra_rules_for(parsed))
