"""The scan eval: accuracy per variant, and whether wrong values were caught or went through."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.evals.run import replay_pipeline
from docforge.evals.scans import ScanReport, format_scan_report, run_scan_eval
from docforge.evals.trust import trust_pipelines
from docforge.extraction.pipeline import ExtractionPipeline, InvoicePipeline
from docforge.extraction.purchase_order import PURCHASE_ORDER_SPEC, PurchaseOrderExtraction
from fakes import MappedParser, ScriptedProvider, cited, reprint

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "synthetic"
RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


class Variant:
    """One pair on disk, with a parser and a model that reproduce it exactly unless edited."""

    def __init__(self, directory: Path, build: RawFromLabel) -> None:
        shutil.copytree(FIXTURES / "pair_001", directory / "pair_001")
        manifest = {"seed": 1, "count": 1, "pairs": ["pair_001"]}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.directory = directory
        self.label = json.loads((directory / "pair_001" / "label.json").read_text(encoding="utf-8"))
        self.raw = build(self.label)
        self.parsed = cited(self.label, "invoice", self.raw)

    def misread(self, path: str, value: str) -> None:
        """The page is read as `value` and the model copies it faithfully."""
        self.parsed = reprint(self.parsed, self.raw, path, value)

    def pipeline(self) -> InvoicePipeline:
        parser = MappedParser()
        parser.add((self.directory / "pair_001" / "invoice.pdf").read_bytes(), self.parsed)
        return InvoicePipeline(parser, ScriptedProvider([json.dumps(self.raw)]))


@pytest.fixture
def variant(tmp_path: Path, raw_invoice_from_label: RawFromLabel) -> Variant:
    return Variant(tmp_path / "scan", raw_invoice_from_label)


def test_a_correctly_read_variant_has_no_errors(variant: Variant) -> None:
    report = run_scan_eval({"scan": variant.directory}, variant.pipeline())

    result = report.variants["scan"]
    assert result.summary.fields.accuracy == 1.0
    assert (result.documents_with_errors, result.silent_errors) == (0, 0)
    assert result.sent_to_review == 0
    assert result.misreads == ()


def test_a_misread_amount_is_wrong_but_caught_by_the_checks(variant: Variant) -> None:
    variant.misread("lines[0].amount", "1806.36")  # printed 1306.36

    result = run_scan_eval({"scan": variant.directory}, variant.pipeline()).variants["scan"]

    assert result.documents_with_errors == 1
    assert (result.errors_sent_to_review, result.silent_errors) == (1, 0)
    assert [(m.pair_id, m.path, m.expected, m.actual) for m in result.misreads] == [
        ("pair_001", "lines[0].amount", "1306.36", "1806.36")
    ]


def test_a_misread_batch_number_goes_through_unnoticed_and_is_counted(variant: Variant) -> None:
    """Nothing on the page contradicts a misread batch number: this is the silent error."""
    variant.misread("lines[0].batch_no", "XGX944O68")

    result = run_scan_eval({"scan": variant.directory}, variant.pipeline()).variants["scan"]

    assert result.documents_with_errors == 1
    assert (result.errors_sent_to_review, result.silent_errors) == (0, 1)


def test_the_text_report_puts_the_variants_side_by_side(variant: Variant) -> None:
    variant.misread("lines[0].batch_no", "XGX944O68")
    report = run_scan_eval({"scan": variant.directory}, variant.pipeline())

    text = format_scan_report(report)

    assert "scan" in text
    assert "1 with a wrong value" in text
    assert "1 of those accepted without review" in text
    assert "pair_001 lines[0].batch_no: expected 'XGX944068', got 'XGX944O68'" in text


def orders(
    variant: Variant, build: RawFromLabel
) -> tuple[Path, ExtractionPipeline[PurchaseOrderExtraction]]:
    """The clean purchase orders and a pipeline that reads them perfectly."""
    raw = build(variant.label)
    parser = MappedParser()
    parser.add(
        (FIXTURES / "pair_001" / "purchase_order.pdf").read_bytes(),
        cited(variant.label, "purchase_order", raw),
    )
    return FIXTURES, ExtractionPipeline(
        parser, ScriptedProvider([json.dumps(raw)]), PURCHASE_ORDER_SPEC
    )


def test_a_misread_product_name_passes_its_own_checks_but_not_the_order_match(
    variant: Variant, raw_order_from_label: RawFromLabel
) -> None:
    variant.misread("lines[0].product_name", "Amoxicilin Capsules IP 250mg")

    result = run_scan_eval(
        {"scan": variant.directory},
        variant.pipeline(),
        orders=orders(variant, raw_order_from_label),
    ).variants["scan"]

    assert result.silent_errors == 1
    assert result.silent_errors_after_order_match == 0


def test_a_misread_batch_number_is_not_caught_by_the_order_match_either(
    variant: Variant, raw_order_from_label: RawFromLabel
) -> None:
    variant.misread("lines[0].batch_no", "XGX944O68")

    result = run_scan_eval(
        {"scan": variant.directory},
        variant.pipeline(),
        orders=orders(variant, raw_order_from_label),
    ).variants["scan"]

    assert result.silent_errors_after_order_match == 1


def test_without_orders_the_match_is_not_reported(variant: Variant) -> None:
    result = run_scan_eval({"scan": variant.directory}, variant.pipeline()).variants["scan"]

    assert result.silent_errors_after_order_match is None


BASELINE = REPO / "evals" / "baselines" / "scans.json"


def committed() -> ScanReport:
    return ScanReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))


def test_the_committed_scan_report_is_reproduced_offline() -> None:
    report = committed()
    recordings = REPO / "tests" / "fixtures" / "recorded"
    scanned = REPO / "tests" / "fixtures" / "scanned"
    variants = {
        "clean": FIXTURES,
        "scan_good": scanned / "scan_good",
        "scan_poor": scanned / "scan_poor",
    }
    _, order_pipeline = trust_pipelines(recordings, report.model)

    replayed = run_scan_eval(
        variants, replay_pipeline(recordings, report.model), orders=(FIXTURES, order_pipeline)
    )

    assert replayed == report


def test_scan_accuracy_does_not_fall_below_what_was_measured() -> None:
    """Lowering a floor is a decision, not a side effect of re-recording."""
    variants = committed().variants

    assert variants["clean"].summary.fields.accuracy == 1.0
    assert variants["scan_good"].summary.fields.accuracy >= 0.999
    assert variants["scan_poor"].summary.fields.accuracy >= 0.98
    assert {variant.source for name, variant in variants.items() if name != "clean"} == {"ocr"}


def test_no_wrong_value_gets_past_both_the_checks_and_the_order_match() -> None:
    assert [v.silent_errors_after_order_match for v in committed().variants.values()] == [0, 0, 0]
