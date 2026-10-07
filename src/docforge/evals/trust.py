"""Run the trust layer over the clean pairs and the seeded cases.

Two questions: does every seeded defect get reported, and how often is a correct document
sent to review anyway?
"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from docforge.extraction.pipeline import (
    INVOICE_SPEC,
    ExtractionPipeline,
    InvoicePipeline,
    PipelineResult,
)
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC, PurchaseOrderExtraction
from docforge.extraction.schema import InvoiceExtraction
from docforge.llm.base import LLMProvider, LLMResponse
from docforge.llm.gemini import GeminiProvider
from docforge.llm.replay import RecordingProvider
from docforge.parsing.cache import CachingParser
from docforge.parsing.docling_parser import DoclingParser
from docforge.synth.models import PairLabel
from docforge.synth.seeded import SeededLabel
from docforge.trust.assess import Assessment, assess
from docforge.trust.match import match_invoice_to_order

Finding = tuple[str, str]  # (kind, code): rule id, discrepancy code or flagged field path


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CleanResult(_Model):
    pair_id: str
    invoice_decision: str
    invoice_reasons: tuple[str, ...]
    order_decision: str
    order_reasons: tuple[str, ...]
    discrepancies: tuple[str, ...]  # error-level differences between invoice and order
    accepted: bool  # both documents accepted and they match: no person needed


class SeededResult(_Model):
    case_id: str
    defect: str
    expected: tuple[Finding, ...]
    found: tuple[Finding, ...]
    caught: bool  # every expected finding was reported
    unexpected: tuple[Finding, ...]  # reported but not expected


class TrustSummary(_Model):
    clean_pairs: int
    clean_pairs_accepted: int  # the rest went to review although nothing is wrong with them
    clean_values_flagged: int
    seeded_cases: int
    seeded_cases_caught: int
    seeded_findings_expected: int
    seeded_findings_caught: int
    seeded_unexpected_findings: int


class TrustUsage(_Model):
    model_calls: int
    input_tokens: int
    output_tokens: int


class TrustReport(_Model):
    provider: str = "gemini"  # whose replies were scored
    model: str
    invoice_prompt_version: str
    order_prompt_version: str
    summary: TrustSummary
    usage: TrustUsage
    clean: tuple[CleanResult, ...]
    seeded: tuple[SeededResult, ...]


def trust_pipelines(
    recordings: Path,
    model: str,
    api_key: str | None = None,
    *,
    provider: LLMProvider | None = None,
    live: bool | None = None,
) -> tuple[InvoicePipeline, ExtractionPipeline[PurchaseOrderExtraction]]:
    """Replay-only without a key; with one, anything not yet recorded is fetched and saved.
    `provider`: another provider's recordings instead of Gemini's (`live` if it records)."""
    live = api_key is not None if live is None else live
    parser = CachingParser(recordings / "parsed", DoclingParser() if live else None)
    if provider is None:
        provider = RecordingProvider(
            recordings / "llm", model, GeminiProvider(model, api_key) if api_key else None
        )
    return (
        InvoicePipeline(parser, provider),
        ExtractionPipeline(parser, provider, PURCHASE_ORDER_SPEC),
    )


def _corrupted(extraction: InvoiceExtraction, path: str, text: str) -> InvoiceExtraction:
    """The extraction as it would be had the model returned `text` for the field at `path`."""
    data: Any = extraction.model_dump(mode="json")
    node = data
    for part in path.replace("]", "").replace("[", ".").split("."):
        node = node[int(part)] if part.isdigit() else node[part]
    node["raw"] = node["value"] = text
    return InvoiceExtraction.model_validate(data)


def _findings(assessment: Assessment, discrepancies: tuple[str, ...]) -> set[Finding]:
    found = {
        ("rule", rule.rule_id)
        for rule in assessment.rules
        if rule.outcome != "passed" and rule.severity == "error"
    }
    found |= {("verification", f.path) for f in assessment.fields if f.status != "verified"}
    return found | {("discrepancy", code) for code in discrepancies}


def run_trust_eval(
    clean_dir: Path,
    seeded_dir: Path,
    invoices: ExtractionPipeline[InvoiceExtraction],
    orders: ExtractionPipeline[PurchaseOrderExtraction],
) -> TrustReport:
    calls: list[LLMResponse] = []

    def extract(
        directory: Path,
    ) -> tuple[PipelineResult[InvoiceExtraction], PipelineResult[PurchaseOrderExtraction]]:
        invoice = invoices.extract(invoices.parser.parse((directory / "invoice.pdf").read_bytes()))
        order = orders.extract(orders.parser.parse((directory / "purchase_order.pdf").read_bytes()))
        calls.extend((*invoice.responses, *order.responses))
        return invoice, order

    def errors(invoice: InvoiceExtraction, order: PurchaseOrderExtraction) -> tuple[str, ...]:
        return tuple(
            d.code for d in match_invoice_to_order(invoice, order) if d.severity == "error"
        )

    clean: list[CleanResult] = []
    flagged = 0
    for directory in sorted(clean_dir.glob("pair_*")):
        label = PairLabel.model_validate_json((directory / "label.json").read_text("utf-8"))
        invoice, order = extract(directory)
        differences = errors(invoice.extraction, order.extraction)
        flagged += len(_findings(invoice.assessment, differences))
        flagged += len(_findings(order.assessment, ()))
        clean.append(
            CleanResult(
                pair_id=label.pair_id,
                invoice_decision=invoice.assessment.decision,
                invoice_reasons=invoice.assessment.reasons,
                order_decision=order.assessment.decision,
                order_reasons=order.assessment.reasons,
                discrepancies=differences,
                accepted=invoice.assessment.decision == "accept"
                and order.assessment.decision == "accept"
                and not differences,
            )
        )
    seeded: list[SeededResult] = []
    for directory in sorted(seeded_dir.glob("case_*")):
        label = SeededLabel.model_validate_json((directory / "label.json").read_text("utf-8"))
        invoice, order = extract(directory)
        extraction, assessment = invoice.extraction, invoice.assessment
        if label.corrupt_reply is not None:
            extraction = _corrupted(extraction, label.corrupt_reply.path, label.corrupt_reply.text)
            assessment = assess(extraction, invoice.parsed, INVOICE_SPEC.rules)
        found = _findings(assessment, errors(extraction, order.extraction))
        expected = {(item.kind, item.code) for item in label.expected}
        seeded.append(
            SeededResult(
                case_id=label.pair_id,
                defect=label.defect,
                expected=tuple(sorted(expected)),
                found=tuple(sorted(found)),
                caught=expected <= found,
                unexpected=tuple(sorted(found - expected)),
            )
        )

    return TrustReport(
        model=invoices.provider.model,
        invoice_prompt_version=INVOICE_SPEC.prompt_version,
        order_prompt_version=PURCHASE_ORDER_SPEC.prompt_version,
        summary=TrustSummary(
            clean_pairs=len(clean),
            clean_pairs_accepted=sum(result.accepted for result in clean),
            clean_values_flagged=flagged,
            seeded_cases=len(seeded),
            seeded_cases_caught=sum(result.caught for result in seeded),
            seeded_findings_expected=sum(len(result.expected) for result in seeded),
            seeded_findings_caught=sum(
                len(set(result.expected) & set(result.found)) for result in seeded
            ),
            seeded_unexpected_findings=sum(len(result.unexpected) for result in seeded),
        ),
        usage=TrustUsage(
            model_calls=len(calls),
            input_tokens=sum(call.input_tokens or 0 for call in calls),
            output_tokens=sum(call.output_tokens or 0 for call in calls),
        ),
        clean=tuple(clean),
        seeded=tuple(seeded),
    )


def format_trust_report(report: TrustReport) -> str:
    s = report.summary
    lines = [
        f"model {report.model}; prompts {report.invoice_prompt_version}, "
        f"{report.order_prompt_version}",
        f"seeded defects: {s.seeded_cases_caught} of {s.seeded_cases} cases caught "
        f"({s.seeded_findings_caught} of {s.seeded_findings_expected} expected findings); "
        f"{s.seeded_unexpected_findings} findings beyond those expected",
        f"clean pairs: {s.clean_pairs_accepted} of {s.clean_pairs} accepted with no review; "
        f"{s.clean_values_flagged} values or checks flagged on correct documents",
    ]
    lines += [
        f"  missed {result.case_id} ({result.defect}): expected {result.expected}, "
        f"found {result.found}"
        for result in report.seeded
        if not result.caught
    ]
    lines += [
        f"  review {r.pair_id}: invoice {list(r.invoice_reasons)}, order {list(r.order_reasons)}, "
        f"discrepancies {list(r.discrepancies)}"
        for r in report.clean
        if not r.accepted
    ]
    return "\n".join(lines)
