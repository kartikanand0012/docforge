"""The scan eval: the same invoices as clean PDFs and as scans, scored the same way.

Besides accuracy it counts what matters for trust: of the documents with a wrong value, how
many the checks sent to review, and how many went through without anyone being told.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from docforge.evals.run import Usage, run_eval
from docforge.evals.scoring import DocumentScore, EvalSummary
from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline, PipelineResult
from docforge.extraction.purchase_order import PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.synth.dataset import ORDER_FILE
from docforge.trust.match import match_invoice_to_order

Result = PipelineResult[InvoiceExtraction]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Misread(_Model):
    """One printed value that was extracted wrongly or not at all."""

    pair_id: str
    path: str
    expected: str | None
    actual: str | None
    outcome: str


class VariantResult(_Model):
    parser: str
    source: str  # text_layer or ocr, as the parser reported for the first document
    summary: EvalSummary
    usage: Usage
    sent_to_review: int  # by the document's own checks; the order match is not part of this
    documents_with_errors: int
    errors_sent_to_review: int
    silent_errors: int  # documents with a wrong value that the checks accepted
    # ...and that also agreed with their purchase order. None if orders were not compared.
    silent_errors_after_order_match: int | None = None
    misreads: tuple[Misread, ...]


class ScanReport(_Model):
    model: str
    prompt_version: str
    variants: dict[str, VariantResult]


def run_scan_eval(
    variants: Mapping[str, Path],
    pipeline: InvoicePipeline,
    on_document: Callable[[str, DocumentScore], None] | None = None,
    orders: tuple[Path, ExtractionPipeline[PurchaseOrderExtraction]] | None = None,
) -> ScanReport:
    """Run the invoice eval on each named fixture directory with the same pipeline.

    With `orders` (the directory of clean pairs and a purchase-order pipeline), each invoice
    is also compared with its order, as it would be in use.
    """
    results: dict[str, VariantResult] = {}
    prompt_version = ""
    for name, directory in variants.items():
        outcomes: list[tuple[DocumentScore, Result | None]] = []

        def collect(
            score: DocumentScore,
            result: Result | None,
            name: str = name,
            outcomes: list[tuple[DocumentScore, Result | None]] = outcomes,
        ) -> None:
            outcomes.append((score, result))
            if on_document is not None:
                on_document(name, score)

        report = run_eval(directory, pipeline, on_result=collect)
        prompt_version = report.prompt_version
        # A document that could not be extracted at all is also one a person must see.
        review = [r is None or r.assessment.decision == "review" for _, r in outcomes]
        wrong = [not score.fully_correct for score, _ in outcomes]
        after_match = None
        if orders is not None:
            mismatch = [_mismatch(score.pair_id, r, orders) for score, r in outcomes]
            after_match = sum(
                w and not r and not m for w, r, m in zip(wrong, review, mismatch, strict=True)
            )
        first = next(iter(sorted(directory.glob("pair_*"))), None)
        source = "text_layer"
        if first is not None:
            source = pipeline.parser.parse((first / "invoice.pdf").read_bytes()).source
        results[name] = VariantResult(
            parser=f"{report.parser.name} {report.parser.version}",
            source=source,
            summary=report.summary,
            usage=report.usage,
            sent_to_review=sum(review),
            documents_with_errors=sum(wrong),
            errors_sent_to_review=sum(w and r for w, r in zip(wrong, review, strict=True)),
            silent_errors=sum(w and not r for w, r in zip(wrong, review, strict=True)),
            silent_errors_after_order_match=after_match,
            misreads=tuple(
                Misread(
                    pair_id=score.pair_id,
                    path=field.path,
                    expected=field.expected,
                    actual=field.actual,
                    outcome=field.outcome,
                )
                for score, _ in outcomes
                for field in score.scored
                if field.outcome != "correct"
            ),
        )
    return ScanReport(
        model=pipeline.provider.model, prompt_version=prompt_version, variants=results
    )


def _mismatch(
    pair_id: str,
    result: Result | None,
    orders: tuple[Path, ExtractionPipeline[PurchaseOrderExtraction]],
) -> bool:
    """Does the invoice disagree with its purchase order in a way that needs review?"""
    if result is None:
        return True
    directory, pipeline = orders
    order = pipeline.run((directory / pair_id / ORDER_FILE).read_bytes()).extraction
    found = match_invoice_to_order(result.extraction, order)
    return any(discrepancy.severity == "error" for discrepancy in found)


def format_scan_report(report: ScanReport) -> str:
    """The variants side by side, then every wrong value."""
    lines = [f"model {report.model}, prompt {report.prompt_version}"]
    for name, result in report.variants.items():
        fields, summary = result.summary.fields, result.summary
        lines += [
            f"{name} ({result.source}):",
            f"  fields {fields.accuracy:.2%} ({fields.correct}/{fields.total} correct, "
            f"{fields.wrong} wrong, {fields.missing} missing)",
            f"  documents fully correct: {summary.documents_fully_correct} of {summary.documents}; "
            f"citations {summary.citations.accuracy:.2%}",
            f"  sent to review by their own checks: {result.sent_to_review} of {summary.documents}",
            f"  {result.documents_with_errors} with a wrong value; "
            f"{result.silent_errors} of those accepted without review"
            + (
                ""
                if result.silent_errors_after_order_match is None
                else f", {result.silent_errors_after_order_match} after the order match"
            ),
            f"  model latency p50 {result.usage.latency_ms_p50 / 1000:.1f} s, "
            f"p95 {result.usage.latency_ms_p95 / 1000:.1f} s",
        ]
    for name, result in report.variants.items():
        lines += [
            f"  {name} {wrong.pair_id} {wrong.path}: expected {wrong.expected!r}, "
            f"got {wrong.actual!r}"
            for wrong in result.misreads
        ]
    return "\n".join(lines)
