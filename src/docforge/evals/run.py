"""Run the pipeline over the labelled pairs and report accuracy, tokens and latency."""

import json
import math
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from docforge.evals.scoring import (
    DocumentScore,
    EvalSummary,
    Tally,
    score_invoice,
    summarize,
)
from docforge.extraction.pipeline import (
    ExtractionError,
    ExtractionPipeline,
    InvoicePipeline,
    PipelineResult,
)
from docforge.extraction.schema import InvoiceExtraction
from docforge.llm.base import LLMProvider, LLMResponse
from docforge.llm.gemini import GeminiProvider
from docforge.llm.replay import RecordingProvider
from docforge.parsing.cache import CachingParser
from docforge.parsing.docling_parser import DoclingParser
from docforge.synth.models import PairLabel

LABEL_FILE = "label.json"
INVOICE_FILE = "invoice.pdf"
MANIFEST_FILE = "manifest.json"
SCHEMA_VERSION = "invoice-1"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DatasetInfo(_Model):
    seed: int
    count: int


class ParserInfo(_Model):
    name: str
    version: str


class Usage(_Model):
    """Model usage for the whole run. Latency is model time per document, without parsing."""

    model_calls: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    pages: int
    latency_ms_p50: float
    latency_ms_p95: float


class EvalReport(_Model):
    dataset: DatasetInfo
    parser: ParserInfo
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    summary: EvalSummary
    usage: Usage
    documents: tuple[DocumentScore, ...]


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def run_eval(
    fixtures: Path,
    pipeline: ExtractionPipeline[InvoiceExtraction],
    on_document: Callable[[DocumentScore], None] | None = None,
    on_result: Callable[[DocumentScore, PipelineResult[InvoiceExtraction] | None], None]
    | None = None,
) -> EvalReport:
    """Extract every `pair_*/invoice.pdf` under `fixtures` and score it against its label.

    A reply that never fits the schema scores that document as all missing. Parser and
    provider errors (including an exhausted quota) stop the run.
    """
    manifest = json.loads((fixtures / MANIFEST_FILE).read_text(encoding="utf-8"))
    directories = sorted(fixtures.glob("pair_*"))
    if [directory.name for directory in directories] != manifest["pairs"]:
        raise ValueError(f"the pairs under {fixtures} do not match its manifest")
    scores: list[DocumentScore] = []
    responses: list[LLMResponse] = []
    latencies: list[float] = []
    pages = 0
    parser_info = ParserInfo(name=pipeline.parser.name, version=pipeline.parser.version)

    for directory in directories:
        label = PairLabel.model_validate_json((directory / LABEL_FILE).read_text(encoding="utf-8"))
        parsed = pipeline.parser.parse((directory / INVOICE_FILE).read_bytes())
        parser_info = ParserInfo(name=parsed.parser, version=parsed.parser_version)
        pages += len(parsed.pages)
        try:
            result = pipeline.extract(parsed)
        except ExtractionError as error:
            calls = error.responses
            score = score_invoice(label, None, parsed, error=str(error))
            outcome = None
        else:
            calls = result.responses
            score = score_invoice(label, result.extraction, parsed)
            outcome = result
        responses.extend(calls)
        latencies.append(sum(call.latency_ms for call in calls))
        scores.append(score)
        if on_document is not None:
            on_document(score)
        if on_result is not None:
            on_result(score, outcome)

    return EvalReport(
        dataset=DatasetInfo(seed=manifest["seed"], count=len(scores)),
        parser=parser_info,
        # From the replies themselves, so a replayed run names the provider that produced them.
        provider=responses[0].provider if responses else pipeline.provider.name,
        model=pipeline.provider.model,
        prompt_version=pipeline.spec.prompt_version,
        schema_version=SCHEMA_VERSION,
        summary=summarize(scores),
        usage=Usage(
            model_calls=len(responses),
            input_tokens=sum(call.input_tokens or 0 for call in responses),
            output_tokens=sum(call.output_tokens or 0 for call in responses),
            thinking_tokens=sum(call.thinking_tokens or 0 for call in responses),
            pages=pages,
            latency_ms_p50=_percentile(latencies, 0.50),
            latency_ms_p95=_percentile(latencies, 0.95),
        ),
        documents=tuple(scores),
    )


def replay_pipeline(
    recordings: Path, model: str, *, provider: LLMProvider | None = None
) -> InvoicePipeline:
    """Offline: cached parses and recorded model replies only (Gemini's, or `provider`'s)."""
    return InvoicePipeline(
        CachingParser(recordings / "parsed"),
        provider or RecordingProvider(recordings / "llm", model),
    )


def record_pipeline(
    recordings: Path, model: str, api_key: str | None = None, *, provider: LLMProvider | None = None
) -> InvoicePipeline:
    """Live for anything not yet recorded; every new parse and reply is saved. Gemini with
    `api_key`, or `provider` (recording another provider's replies)."""
    if provider is None:
        if api_key is None:
            raise ValueError("recording Gemini needs its key")
        provider = RecordingProvider(recordings / "llm", model, GeminiProvider(model, api_key))
    return InvoicePipeline(CachingParser(recordings / "parsed", DoclingParser()), provider)


def format_report(report: EvalReport) -> str:
    """The headline numbers as plain text."""
    summary, usage = report.summary, report.usage

    def row(name: str, tally: Tally) -> str:
        counts = f"{tally.correct}/{tally.total} correct, {tally.wrong} wrong"
        return f"  {name:<12}{tally.accuracy:>8.2%}  {counts}, {tally.missing} missing"

    per_document = max(report.dataset.count, 1)
    lines = [
        f"model {report.model} ({report.provider}), prompt {report.prompt_version}, "
        f"parser {report.parser.name} {report.parser.version}",
        f"documents: {summary.documents}, fully correct: {summary.documents_fully_correct}, "
        f"line count right: {summary.line_count_matches}, invented lines: {summary.extra_lines}",
        "field accuracy (printed fields):",
        row("all", summary.fields),
        *(row(name, tally) for name, tally in summary.by_class.items()),
        *(row(f"layout {name}", tally) for name, tally in summary.by_layout.items()),
        f"citations: {summary.citations.accuracy:.2%} of {summary.citations.checked} values "
        "cite a block covering their true position",
        f"values given for unprinted tax fields: {summary.null_expected.hallucinated} "
        f"of {summary.null_expected.total}",
        f"model calls: {usage.model_calls}; tokens per document: "
        f"{usage.input_tokens // per_document} in, {usage.output_tokens // per_document} out",
        f"model latency per document: p50 {usage.latency_ms_p50 / 1000:.1f} s, "
        f"p95 {usage.latency_ms_p95 / 1000:.1f} s",
    ]
    return "\n".join(lines)
