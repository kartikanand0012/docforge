"""The search eval: recall per mode, per tenant, and nothing found across tenants."""

from pathlib import Path

import pytest

from docforge.evals.search import build_questions, format_search_report, run_search_eval
from docforge.search.embeddings import FakeEmbedder

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
SYNTHETIC = REPO / "tests" / "fixtures" / "synthetic"
COA = REPO / "tests" / "fixtures" / "coa"
RECORDED = REPO / "tests" / "fixtures" / "recorded"


def test_questions_are_generated_from_the_labels_with_their_answers() -> None:
    questions = build_questions(SYNTHETIC, COA)

    assert len(questions) >= 100
    assert {q.kind for q in questions} >= {"exact", "paraphrase", "certificate"}
    assert all(q.expected and q.tenant in {"a", "b"} for q in questions)
    batch = next(q for q in questions if q.text.startswith("Which invoice billed batch"))
    assert batch.expected == ("pair_001/invoice",)
    assert build_questions(SYNTHETIC, COA) == questions  # repeatable


def test_the_eval_reports_recall_per_mode_and_no_leak_across_tenants(
    empty_database_url: object,
) -> None:
    report = run_search_eval(
        SYNTHETIC, COA, RECORDED, FakeEmbedder(), pairs=2, database_url=empty_database_url
    )

    assert set(report.modes) == {"keyword", "vector", "hybrid"}
    for mode in report.modes.values():
        assert 0.0 <= mode.recall_at_5_filtered <= 1.0
        assert 0.0 <= mode.recall_at_5_unfiltered <= 1.0
        assert mode.cross_tenant_hits == 0
    assert report.modes["keyword"].by_kind["exact"] == 1.0  # numbers are found by their words
    assert report.documents == 6 and report.questions > 0
    assert "recall@5" in format_search_report(report)
