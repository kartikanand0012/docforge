"""The real-invoice eval: approved readings are the truth, a fresh reading is scored against them.

Labels come from DocForge itself: the owner uploads real bills, corrects every field in review
and approves; `pull` turns the approved records into labels next to the PDFs (under `data/`,
which git ignores), and `run` reads each PDF again and scores it field by field.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from docforge.evals.real import (
    labels_from_export,
    pull,
    run_real_eval,
    score_against_record,
    shareable_summary,
)
from docforge.extraction.pipeline import InvoicePipeline
from docforge.extraction.schema import InvoiceExtraction
from fakes import FakeParser, ScriptedProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
PDF = (FIXTURES / "pair_001" / "invoice.pdf").read_bytes()
LABEL = json.loads((FIXTURES / "pair_001" / "label.json").read_text(encoding="utf-8"))

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def raw_reply(raw_invoice_from_label: RawFromLabel) -> str:
    return json.dumps(raw_invoice_from_label(LABEL))


@pytest.fixture
def reading(raw_reply: str) -> InvoiceExtraction:
    return InvoicePipeline(FakeParser(), ScriptedProvider([raw_reply])).run(PDF).extraction


def record(extraction: InvoiceExtraction) -> dict[str, Any]:
    return extraction.model_dump(mode="json")


def with_value(data: dict[str, Any], path: list[Any], value: Any) -> dict[str, Any]:
    copy = json.loads(json.dumps(data))
    node = copy
    for part in path:
        node = node[part]
    node["value"] = value
    return copy


def labelled(tmp_path: Path, reading: InvoiceExtraction, names: tuple[str, ...]) -> tuple[Path, Path]:
    pdfs, labels = tmp_path / "pdfs", tmp_path / "labels"
    pdfs.mkdir()
    labels.mkdir()
    for name in names:
        (labels / f"{name}.json").write_text(
            json.dumps({"document_id": "d", "filename": name, "record": record(reading)}),
            encoding="utf-8",
        )
    return pdfs, labels


def test_a_reading_equal_to_the_approved_record_scores_every_printed_field_correct(
    reading: InvoiceExtraction,
) -> None:
    score = score_against_record("bill-1", record(reading), reading)

    assert score.fields
    assert {field.outcome for field in score.fields} == {"correct"}
    assert score.expected_lines == score.extracted_lines == len(reading.lines)


def test_wrong_missing_and_invented_values_are_told_apart(reading: InvoiceExtraction) -> None:
    truth = with_value(record(reading), ["lines", 0, "batch_no"], "B-REAL-1")  # corrected
    truth = with_value(truth, ["totals", "igst"], None)  # not printed
    actual = with_value(record(reading), ["lines", 1, "expiry"], None)
    actual = with_value(actual, ["totals", "igst"], "12.00")

    score = score_against_record("bill-1", truth, InvoiceExtraction.model_validate(actual))
    by_path = {field.path: field for field in score.fields}

    assert by_path["lines[0].batch_no"].outcome == "wrong"
    assert by_path["lines[1].expiry"].outcome == "missing"
    assert by_path["totals.igst"].outcome == "invented"
    assert by_path["lines[0].batch_no"].field_class == "identifier"


def test_a_field_empty_in_both_is_not_counted(reading: InvoiceExtraction) -> None:
    truth = with_value(record(reading), ["po_date"], None)
    actual = InvoiceExtraction.model_validate(truth)

    paths = {field.path for field in score_against_record("bill-1", truth, actual).fields}

    assert "po_date" not in paths


def test_missing_and_extra_lines_are_counted(reading: InvoiceExtraction) -> None:
    truth = record(reading)
    shorter = InvoiceExtraction.model_validate({**truth, "lines": truth["lines"][:-1]})

    missing = score_against_record("bill-1", truth, shorter)
    extra = score_against_record("bill-1", {**truth, "lines": truth["lines"][:-1]}, reading)

    assert missing.extracted_lines == missing.expected_lines - 1
    last = len(truth["lines"]) - 1
    assert {f.outcome for f in missing.fields if f.path.startswith(f"lines[{last}]")} == {
        "missing"
    }
    assert extra.extra_lines == 1


def test_a_failed_reading_scores_every_expected_field_missing(reading: InvoiceExtraction) -> None:
    score = score_against_record("bill-1", record(reading), None, error="model failed")

    assert {field.outcome for field in score.fields} == {"missing"}
    assert score.error == "model failed"


def export_row(document_id: str, filename: str, signed_at: str, **overrides: Any) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "doc_type": "invoice",
        "filename": filename,
        "outcome": "approved",
        "signed_at": signed_at,
        "record": {"invoice_no": {"value": filename}},
        **overrides,
    }


def test_labels_are_the_newest_approval_of_each_invoice() -> None:
    rows = [  # the export is newest first
        export_row("d1", "bill-a.pdf", "2026-10-14T10:00:00+00:00"),
        export_row("d1", "bill-a.pdf", "2026-10-13T10:00:00+00:00", record={"old": True}),
        export_row("d2", "bill-b.pdf", "2026-10-14T09:00:00+00:00", outcome="rejected"),
        export_row("d3", "order.pdf", "2026-10-14T09:00:00+00:00", doc_type="purchase_order"),
    ]

    labels = labels_from_export(rows)

    assert list(labels) == ["bill-a.pdf"]
    assert labels["bill-a.pdf"]["record"] == {"invoice_no": {"value": "bill-a.pdf"}}


def test_a_document_whose_newest_signature_is_a_rejection_is_not_a_label() -> None:
    rows = [
        export_row("d1", "bill-a.pdf", "2026-10-14T10:00:00+00:00", outcome="rejected"),
        export_row("d1", "bill-a.pdf", "2026-10-13T10:00:00+00:00"),
    ]

    assert labels_from_export(rows) == {}


def test_two_approved_documents_with_one_filename_are_refused() -> None:
    rows = [
        export_row("d1", "bill.pdf", "2026-10-14T10:00:00+00:00"),
        export_row("d2", "bill.pdf", "2026-10-14T09:00:00+00:00"),
    ]

    with pytest.raises(ValueError, match=r"bill\.pdf"):
        labels_from_export(rows)


def test_pull_writes_one_label_per_approved_invoice_with_the_key_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        row = export_row("d1", "bill-a.pdf", "2026-10-14T10:00:00+00:00")
        return httpx.Response(200, json=[row])

    monkeypatch.setenv("DOCFORGE_API_KEY", "dfk_test")

    written = pull(
        "https://docforge.example", tmp_path / "labels", transport=httpx.MockTransport(handler)
    )

    assert written == 1
    assert seen[0].url.path == "/v1/exports/documents.json"
    assert seen[0].headers["authorization"] == "Bearer dfk_test"
    label = json.loads((tmp_path / "labels" / "bill-a.pdf.json").read_text(encoding="utf-8"))
    assert label["document_id"] == "d1"


@pytest.mark.parametrize("url", ["http://docforge.example", "ftp://docforge.example"])
def test_pull_refuses_to_send_the_key_without_https(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCFORGE_API_KEY", "dfk_test")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))

    with pytest.raises(ValueError, match="https"):
        pull(url, tmp_path, transport=transport)


def test_pull_needs_the_key_in_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOCFORGE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="DOCFORGE_API_KEY"):
        pull("https://docforge.example", tmp_path)


def test_a_label_named_to_climb_out_of_the_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCFORGE_API_KEY", "dfk_test")
    row = export_row("d1", "../../escape.pdf", "2026-10-14T10:00:00+00:00")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[row]))

    with pytest.raises(ValueError, match="filename"):
        pull("https://docforge.example", tmp_path / "labels", transport=transport)
    assert not (tmp_path / "escape.pdf.json").exists()


def test_run_reads_each_labelled_pdf_and_says_which_are_unmatched(
    tmp_path: Path, reading: InvoiceExtraction, raw_reply: str
) -> None:
    pdfs, labels = labelled(tmp_path, reading, ("bill-a.pdf", "no-pdf.pdf"))
    (pdfs / "bill-a.pdf").write_bytes(PDF)
    (pdfs / "unlabelled.pdf").write_bytes(PDF)

    pipeline = InvoicePipeline(FakeParser(), ScriptedProvider([raw_reply]))
    report = run_real_eval(pdfs, labels, pipeline)

    assert [document.name for document in report.documents] == ["bill-a.pdf"]
    assert report.unlabelled == ("unlabelled.pdf",)
    assert report.without_pdf == ("no-pdf.pdf",)
    assert report.summary.fields.accuracy == 1.0
    assert report.summary.documents_fully_correct == 1


def test_a_reading_that_fails_is_scored_not_raised(
    tmp_path: Path, reading: InvoiceExtraction
) -> None:
    pdfs, labels = labelled(tmp_path, reading, ("bill-a.pdf",))
    (pdfs / "bill-a.pdf").write_bytes(PDF)

    pipeline = InvoicePipeline(FakeParser(), ScriptedProvider(["not json", "still not json"]))
    report = run_real_eval(pdfs, labels, pipeline)

    (document,) = report.documents
    assert document.error
    assert report.summary.fields.accuracy == 0.0


def test_the_shareable_summary_holds_counts_only(
    tmp_path: Path, reading: InvoiceExtraction, raw_reply: str
) -> None:
    pdfs, labels = labelled(tmp_path, reading, ("bill-a.pdf",))
    (pdfs / "bill-a.pdf").write_bytes(PDF)
    pipeline = InvoicePipeline(FakeParser(), ScriptedProvider([raw_reply]))
    report = run_real_eval(pdfs, labels, pipeline)

    shared = json.dumps(shareable_summary(report))

    for secret in ("bill-a", reading.seller.gstin.value, reading.invoice_no.value):
        assert secret
        assert secret not in shared
    assert '"documents": 1' in shared
