"""The eval runner, and the committed baseline staying reproducible from recordings."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from docforge.evals.__main__ import main
from docforge.evals.run import EvalReport, format_report, replay_pipeline, run_eval
from docforge.extraction.pipeline import InvoicePipeline
from docforge.extraction.prompt import PROMPT_VERSION
from docforge.llm.base import LLMQuotaExhausted
from fakes import FakeParser, ScriptedProvider

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "synthetic"
RECORDINGS = REPO / "tests" / "fixtures" / "recorded"
BASELINE = REPO / "evals" / "baselines" / "invoice.json"

RawFromLabel = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def two_pairs(tmp_path: Path) -> Path:
    for pair_id in ("pair_001", "pair_002"):
        shutil.copytree(FIXTURES / pair_id, tmp_path / pair_id)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"seed": 1, "count": 2, "pairs": ["pair_001", "pair_002"]}), encoding="utf-8"
    )
    return tmp_path


def perfect_replies(fixtures: Path, build: RawFromLabel) -> list[str | Exception]:
    return [
        json.dumps(build(json.loads((directory / "label.json").read_text(encoding="utf-8"))))
        for directory in sorted(fixtures.glob("pair_*"))
    ]


def test_runs_every_pair_and_reports_accuracy_and_usage(
    two_pairs: Path, raw_invoice_from_label: RawFromLabel
) -> None:
    provider = ScriptedProvider(perfect_replies(two_pairs, raw_invoice_from_label))

    report = run_eval(two_pairs, InvoicePipeline(FakeParser(), provider))

    assert [document.pair_id for document in report.documents] == ["pair_001", "pair_002"]
    assert report.summary.documents == 2
    assert report.summary.fields.accuracy == 1.0
    assert (report.provider, report.model) == ("fake", "fake-1")
    assert report.prompt_version == PROMPT_VERSION
    assert report.schema_version == "invoice-1"
    assert (report.parser.name, report.parser.version) == ("fake", "0")
    assert report.dataset.count == 2
    assert report.usage.model_calls == 2
    assert (report.usage.input_tokens, report.usage.output_tokens) == (200, 100)
    assert report.usage.pages == 2
    assert report.usage.latency_ms_p50 == 1.0
    assert report.usage.latency_ms_p95 == 1.0


def test_a_reply_that_never_fits_the_schema_scores_the_document_as_missing(
    two_pairs: Path, raw_invoice_from_label: RawFromLabel
) -> None:
    replies = perfect_replies(two_pairs, raw_invoice_from_label)
    provider = ScriptedProvider(["not json", "{}", replies[1]])

    report = run_eval(two_pairs, InvoicePipeline(FakeParser(), provider))

    failed, passed = report.documents
    assert failed.error is not None
    assert {field.outcome for field in failed.scored} == {"missing"}
    assert passed.error is None
    assert report.summary.documents_fully_correct == 1
    assert report.usage.model_calls == 3


def test_an_exhausted_quota_stops_the_run(two_pairs: Path) -> None:
    provider = ScriptedProvider([LLMQuotaExhausted("daily quota used up")])

    with pytest.raises(LLMQuotaExhausted):
        run_eval(two_pairs, InvoicePipeline(FakeParser(), provider))


def test_report_survives_a_json_round_trip(
    two_pairs: Path, raw_invoice_from_label: RawFromLabel
) -> None:
    provider = ScriptedProvider(perfect_replies(two_pairs, raw_invoice_from_label))
    report = run_eval(two_pairs, InvoicePipeline(FakeParser(), provider))

    assert EvalReport.model_validate_json(report.model_dump_json()) == report


def test_text_summary_shows_the_headline_numbers(
    two_pairs: Path, raw_invoice_from_label: RawFromLabel
) -> None:
    provider = ScriptedProvider(perfect_replies(two_pairs, raw_invoice_from_label))

    text = format_report(run_eval(two_pairs, InvoicePipeline(FakeParser(), provider)))

    assert "fake-1" in text
    assert "100.00%" in text
    assert "identifier" in text
    assert "p95" in text


def test_cli_replay_without_recordings_fails_with_a_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"

    code = main(["--recordings", str(tmp_path / "none"), "--out", str(out)])

    assert code == 1
    assert "make eval-record" in capsys.readouterr().err
    assert not out.exists()


def test_committed_baseline_is_reproduced_from_the_recordings() -> None:
    """Offline and deterministic: no Docling, no network. This is the C1 gate in CI."""
    committed = EvalReport.model_validate_json(BASELINE.read_text(encoding="utf-8"))

    report = run_eval(FIXTURES, replay_pipeline(RECORDINGS, committed.model))

    assert report == committed
    assert report.summary.documents == 20


def test_committed_baseline_meets_the_accepted_floor() -> None:
    """Re-recording a worse run must fail here, not pass silently. Lower it deliberately."""
    summary = EvalReport.model_validate_json(BASELINE.read_text(encoding="utf-8")).summary

    assert summary.documents == 20
    assert summary.documents_fully_correct == 20
    assert (summary.fields.wrong, summary.fields.missing) == (0, 0)
    assert summary.extra_lines == 0
    assert summary.null_expected.hallucinated == 0
    assert summary.citations.accuracy >= 0.99


def test_a_dataset_that_does_not_match_its_manifest_is_refused(
    two_pairs: Path, raw_invoice_from_label: RawFromLabel
) -> None:
    shutil.rmtree(two_pairs / "pair_002")
    (two_pairs / "manifest.json").write_text(
        json.dumps({"seed": 1, "count": 2, "pairs": ["pair_001", "pair_002"]}), encoding="utf-8"
    )
    provider = ScriptedProvider(perfect_replies(two_pairs, raw_invoice_from_label))

    with pytest.raises(ValueError, match="manifest"):
        run_eval(two_pairs, InvoicePipeline(FakeParser(), provider))


def test_report_counts_thinking_tokens(
    two_pairs: Path, raw_invoice_from_label: RawFromLabel
) -> None:
    provider = ScriptedProvider(perfect_replies(two_pairs, raw_invoice_from_label))

    report = run_eval(two_pairs, InvoicePipeline(FakeParser(), provider))

    assert report.usage.thinking_tokens == 0
